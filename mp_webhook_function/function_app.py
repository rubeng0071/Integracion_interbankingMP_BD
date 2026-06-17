"""Azure Functions v2 — Mercado Pago: webhook + worker + poller incremental.

Tres funciones en la misma Function App, conectadas por la queue `mp-payment-ids`:

    POST /api/mp/webhook        →  mp_webhook (HTTP trigger)
        - Valida HMAC + anti-replay (AZ-02).
        - Filtra event_type != "payment".
        - Encola el payment_id.
        - Devuelve 202 Accepted en <200ms.

    Queue "mp-payment-ids"      →  mp_process_payment (Queue trigger)
        - Hidrata el payment con GET /v1/payments/{id}.
        - UPSERT idempotente con AZ-03.
        - Si falla, el runtime reintenta (default: 5 veces con backoff).

    Timer (cada 30 min default) →  mp_poller_run (Timer trigger)
        - Pagina GET /v1/payments/search sobre `date_last_updated` en el rango
          [ahora - MP_INCREMENTAL_LOOKBACK_HOURS, ahora].
        - Encola cada payment_id en `mp-payment-ids`; el worker existente hidrata
          y persiste. Sin código duplicado de upsert.
        - Si MP_INITIAL_LOAD=true, usa `MP_INITIAL_LOOKBACK_DAYS` (default 365)
          para una pasada histórica completa.
        - Idempotencia garantizada por el upsert del worker (`_is_already_current`).

Por qué webhook + poller juntos:
    El webhook captura cambios en tiempo real pero MP no garantiza entrega
    (puede haber webhooks perdidos, rate limits, downtime). El poller es la
    red de seguridad: cada 30 min recorre el rango reciente y encola lo que
    haya. Si el webhook ya procesó algo, el upsert lo detecta como idempotente
    y skipea (`_is_already_current` con date_last_updated).

Referencias:
    Formato HMAC MP: https://www.mercadopago.com.ar/developers/es/docs/your-integrations/notifications/webhooks
    OAuth2 client_credentials: doc Rapanui sección 2.
    Search paginado: doc Rapanui sección 5.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import azure.functions as func
import pyodbc

from shared.config import ConfigError, MpWebhookConfig
from shared.observability import configure_logging
from shared.secret_string import SecretString

from mp_client import MercadoPagoClient, MercadoPagoError
from mp_processor import upsert_payment

logger = logging.getLogger(__name__)


# =====================================================================
# Inicialización perezosa de la config + observability.
# Se hace en module scope con caching para reusar entre invocaciones "warm".
# =====================================================================

_cached_config: Optional[MpWebhookConfig] = None
_cached_mp_client: Optional[MercadoPagoClient] = None


def _get_config() -> MpWebhookConfig:
    global _cached_config
    if _cached_config is None:
        try:
            _cached_config = MpWebhookConfig.from_env()
        except ConfigError as exc:
            logger.error("Config inválida al arrancar la Function: %s", exc)
            raise
        # Bootstrap del logging estructurado + AppInsights ni bien tenemos
        # la conn string. configure_logging es idempotente, así que las
        # invocaciones warm no la reinicializan.
        configure_logging(
            connection_string=_cached_config.application_insights_connection_string,
            level=_cached_config.log_level,
            service_name="mp_webhook",
        )
    return _cached_config


def _get_mp_client() -> MercadoPagoClient:
    """Cliente MP único reusado entre invocaciones. El token OAuth queda cacheado adentro."""
    global _cached_mp_client
    if _cached_mp_client is None:
        cfg = _get_config()
        _cached_mp_client = MercadoPagoClient(
            client_id=cfg.mp_client_id,
            client_secret=cfg.mp_client_secret,
            access_token_override=cfg.mp_access_token,
        )
    return _cached_mp_client


# =====================================================================
# Nombre de la queue (configurable). Default consistente con docs.
# =====================================================================

QUEUE_NAME = os.getenv("MP_PAYMENT_QUEUE_NAME", "mp-payment-ids")


# =====================================================================
# Schedule del poller incremental.
# Default cada 30 min para complementar el webhook sin saturar la queue.
# =====================================================================

_POLLER_SCHEDULE = os.getenv("MP_POLLER_SCHEDULE", "0 */30 * * * *")


# =====================================================================
# HMAC validation (AZ-02)
# =====================================================================

def _parse_x_signature(header_value: str) -> Tuple[Optional[str], Optional[str]]:
    """Parsea el header x-signature de MP que viene como: 'ts=123456789,v1=abcdef...'

    Returns:
        (ts, v1) o (None, None) si el formato es inválido.
    """
    ts: Optional[str] = None
    v1: Optional[str] = None
    try:
        for part in header_value.split(","):
            key, _, value = part.strip().partition("=")
            key = key.strip()
            value = value.strip()
            if key == "ts":
                ts = value
            elif key == "v1":
                v1 = value
    except Exception as exc:
        logger.warning("_parse_x_signature: formato inválido (%s)", type(exc).__name__)
        return None, None
    return ts, v1


def _compute_manifest(data_id: str, request_id: str, ts: str) -> str:
    """Template exacto que MP especifica para el HMAC."""
    return f"id:{data_id};request-id:{request_id};ts:{ts};"


def _verify_signature(
    secret: SecretString,
    data_id: str,
    x_signature_header: Optional[str],
    x_request_id_header: Optional[str],
    max_age_seconds: int = 300,
) -> Tuple[bool, str]:
    """Valida la firma HMAC del webhook.

    Returns:
        (ok, reason). `reason` es un string corto para logging (nunca incluye
        valores sensibles como el HMAC esperado).
    """
    if not x_signature_header:
        return False, "missing_x_signature"
    if not x_request_id_header:
        return False, "missing_x_request_id"
    if not data_id:
        return False, "missing_data_id"

    ts, v1 = _parse_x_signature(x_signature_header)
    if not ts or not v1:
        return False, "malformed_x_signature"

    # Protección contra replay: rechazar eventos con ts muy viejo.
    try:
        ts_int = int(ts)
        now_ms = int(time.time() * 1000)
        # MP envía ts en milisegundos.
        age_seconds = (now_ms - ts_int) / 1000.0
        if age_seconds > max_age_seconds:
            return False, f"ts_too_old({int(age_seconds)}s)"
        if age_seconds < -max_age_seconds:
            return False, f"ts_in_future({int(age_seconds)}s)"
    except ValueError:
        return False, "ts_not_integer"

    manifest = _compute_manifest(str(data_id), x_request_id_header, ts)
    expected = hmac.new(
        key=secret.reveal().encode("utf-8"),
        msg=manifest.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, v1):
        return False, "hmac_mismatch"

    return True, "ok"


# =====================================================================
# Azure Functions v2 — Function App
# =====================================================================

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)


@app.route(route="mp/webhook", methods=["POST"])
@app.queue_output(
    arg_name="msg_out",
    queue_name=QUEUE_NAME,
    connection="AzureWebJobsStorage",
)
def mp_webhook(req: func.HttpRequest, msg_out: func.Out[str]) -> func.HttpResponse:
    """HTTP trigger: valida HMAC y encola el payment_id.

    Responde 202 en cuanto el mensaje se encolá. El upsert real lo hace
    mp_process_payment desde la queue, sin contar contra el SLA del webhook.
    """
    invocation_id = req.headers.get("x-ms-invocation-id", "unknown")

    # 1. Parsear body.
    try:
        body_bytes = req.get_body()
        body = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
    except (ValueError, UnicodeDecodeError) as exc:
        logger.warning("[%s] body JSON inválido: %s", invocation_id, type(exc).__name__)
        return func.HttpResponse("invalid json", status_code=400)

    data_id = str(body.get("data", {}).get("id") or body.get("id") or "")
    event_type = body.get("type") or body.get("topic") or ""

    # 2. AZ-02 — HMAC validation.
    try:
        config = _get_config()
    except ConfigError:
        return func.HttpResponse("config_error", status_code=500)

    ok, reason = _verify_signature(
        secret=config.mp_webhook_secret,
        data_id=data_id,
        x_signature_header=req.headers.get("x-signature"),
        x_request_id_header=req.headers.get("x-request-id"),
    )
    if not ok:
        logger.warning("[%s] HMAC inválido (%s) para event_type=%s", invocation_id, reason, event_type)
        return func.HttpResponse("unauthorized", status_code=401)

    # 3. Filtro por tipo.
    if event_type != "payment":
        logger.info("[%s] event_type=%s ignorado (solo procesamos 'payment')", invocation_id, event_type)
        return func.HttpResponse(
            json.dumps({"status": "ignored", "type": event_type}),
            status_code=200,
            mimetype="application/json",
        )

    if not data_id:
        return func.HttpResponse("missing_payment_id", status_code=400)

    # 4. Encolar: el queue_output binding se materializa cuando set() devuelve.
    # El mensaje es solo el payment_id; el worker lo hidrata via API.
    msg_out.set(data_id)
    logger.info("[%s] payment_id=%s encolado en %s", invocation_id, data_id, QUEUE_NAME)

    return func.HttpResponse(
        json.dumps({"status": "queued", "payment_id": data_id}),
        status_code=202,
        mimetype="application/json",
    )


@app.queue_trigger(
    arg_name="msg",
    queue_name=QUEUE_NAME,
    connection="AzureWebJobsStorage",
)
def mp_process_payment(msg: func.QueueMessage) -> None:
    """Queue trigger: hidrata el payment y hace el upsert.

    El runtime de Functions garantiza at-least-once delivery con backoff
    exponencial automático (default 5 intentos). Si el mensaje sigue
    fallando, va a la queue de poison (<queue>-poison) para inspección
    manual sin bloquear el resto del stream.

    AZ-03: el upsert es idempotente; reintentos de un mismo payment_id
    no duplican.
    """
    payment_id = msg.get_body().decode("utf-8").strip()
    invocation_id = getattr(msg, "id", "unknown")

    if not payment_id:
        logger.warning("[%s] mensaje vacío en %s; descartando", invocation_id, QUEUE_NAME)
        return

    try:
        config = _get_config()
    except ConfigError:
        logger.exception("[%s] config_error procesando payment_id=%s", invocation_id, payment_id)
        raise

    try:
        payment = _get_mp_client().get_payment(payment_id)
    except MercadoPagoError:
        logger.exception("[%s] GET /v1/payments/%s falló", invocation_id, payment_id)
        # Re-raise: el runtime hace retry con backoff y eventualmente a poison.
        raise

    try:
        with pyodbc.connect(config.sql_connection_string.reveal(), autocommit=False) as conn:
            try:
                result = upsert_payment(conn, payment)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    except pyodbc.Error:
        logger.exception("[%s] Error de DB procesando payment %s", invocation_id, payment_id)
        raise

    logger.info(
        "[%s] payment %s procesado: skipped=%s charges=%d items=%d",
        invocation_id,
        result.payment_id,
        result.skipped_idempotent,
        result.charges_upserted,
        result.items_upserted,
    )


# =====================================================================
# Poller incremental (red de seguridad / backfill histórico)
# =====================================================================

# Cap defensivo: nunca pedir más que esto por ciclo, para no quemar el
# functionTimeout si MP devuelve un universo gigante por error.
_MAX_PAGES_PER_RUN = 200    # 200 * 50 = 10_000 pagos por ciclo, suficiente para un día normal.


def _poller_window(config: MpWebhookConfig, now: Optional[datetime] = None) -> Tuple[datetime, datetime]:
    """Calcula la ventana [begin, end] que va a poll-ear este ciclo.

    - Modo incremental (default): ventana `[now - lookback_hours, now]`.
    - Modo initial-load: ventana `[now - lookback_days, now]` (carga histórica).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if config.mp_initial_load:
        begin = now - timedelta(days=config.mp_initial_lookback_days)
        logger.info("mp_poller: modo INITIAL_LOAD (lookback=%dd)", config.mp_initial_lookback_days)
    else:
        begin = now - timedelta(hours=config.mp_incremental_lookback_hours)
    return begin, now


def collect_payment_ids(
    client: MercadoPagoClient,
    begin: datetime,
    end: datetime,
    page_delay_seconds: float = 0.2,
    max_pages: int = _MAX_PAGES_PER_RUN,
    page_size: int = 50,
) -> List[str]:
    """Recolecta los payment_ids del rango sin duplicados, vía slicing por fecha.

    Delega en `client.iter_all_payments`, que esquiva el cap de offset 10_000 de MP
    partiendo el rango de fechas (sin esto, una ventana con >10_000 matches perdía el
    resto en silencio). El poller incremental usa `range=date_last_updated` para
    capturar tanto altas como actualizaciones.

    Función pura (sin side effects de queue/SQL) para que sea testeable. El timer
    trigger la invoca y después delega el encolado al binding queue_output.

    Cap defensivo: `max_pages * page_size` ids por ciclo. Evita quemar el
    functionTimeout si una ventana trae un universo inesperadamente grande; el resto
    entra en los próximos ciclos (o se hace via backfill dedicado).
    """
    max_ids = max_pages * page_size
    enqueued: List[str] = []

    try:
        for payment in client.iter_all_payments(
            begin=begin,
            end=end,
            range_field="date_last_updated",
            page_size=page_size,
            page_delay_seconds=page_delay_seconds,
        ):
            pid = payment.get("id")
            if pid is None:
                continue
            enqueued.append(str(pid))
            if len(enqueued) >= max_ids:
                logger.warning(
                    "mp_poller: cap de %d ids alcanzado en [%s, %s]; corte defensivo "
                    "(el resto entra en próximos ciclos)",
                    max_ids, begin.isoformat(), end.isoformat(),
                )
                break
    except MercadoPagoError:
        logger.exception(
            "mp_poller: search falló; abortando ciclo (lo ya recolectado se encola igual)"
        )

    logger.info(
        "mp_poller: ventana [%s, %s] encolados=%d",
        begin.isoformat(), end.isoformat(), len(enqueued),
    )
    return enqueued


@app.timer_trigger(
    schedule=_POLLER_SCHEDULE,
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
@app.queue_output(
    arg_name="msg_out",
    queue_name=QUEUE_NAME,
    connection="AzureWebJobsStorage",
)
def mp_poller_run(timer: func.TimerRequest, msg_out: func.Out[List[str]]) -> None:
    """Timer trigger: pagina /v1/payments/search y encola payment_ids.

    No hidrata ni persiste directamente: solo encola. El worker `mp_process_payment`
    es la única ruta de escritura a SQL, así garantizamos idempotencia y
    no duplicamos lógica.
    """
    if timer.past_due:
        logger.warning("mp_poller: timer past_due; arrancando igual")

    try:
        config = _get_config()
    except ConfigError:
        raise

    begin, end = _poller_window(config)
    delay_seconds = max(config.mp_search_page_delay_ms, 0) / 1000.0
    enqueued = collect_payment_ids(
        client=_get_mp_client(),
        begin=begin,
        end=end,
        page_delay_seconds=delay_seconds,
    )

    if not enqueued:
        logger.info(
            "mp_poller: ventana [%s, %s] sin pagos para encolar",
            begin.isoformat(), end.isoformat(),
        )
        return

    # Materializar la encolada en batch. El binding queue_output con List[str]
    # encola un mensaje por elemento de la lista.
    msg_out.set(enqueued)
