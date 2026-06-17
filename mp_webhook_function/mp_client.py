"""Cliente HTTP para la API de Mercado Pago con OAuth2 client_credentials.

Flujo de autenticación:
    1. Al primer request, hace POST /oauth/token con client_id + client_secret y
       grant_type=client_credentials.
    2. Cachea el access_token en memoria con su `expires_at`.
    3. Renueva proactivamente al llegar al 80% del `expires_in` (default 21600s = 6h,
       o sea refresca a las ~4h48m). Evita el caso "request justo al filo de la expiración".
    4. Si recibe 401 con un token cacheado, invalida el cache y reintenta UNA vez
       (puede pasar si el secret fue rotado).

Override para dev local:
    Si se pasa `access_token_override`, el cliente lo usa directo y nunca llama a
    /oauth/token. Útil para tests y para usar tokens APP_USR estáticos del panel MP
    sin tener que registrar una app OAuth.

Endpoints expuestos:
    - GET /v1/payments/{id}        → hidratar un payment a partir de un webhook.
    - GET /v1/payments/search      → búsqueda paginada por rango de fechas
                                     (lo usa mp_poller_run para backfill incremental).

Thread safety:
    Functions Python puede ejecutar varias invocaciones concurrentes en el mismo
    worker (queue trigger paralelo, batch processing). El refresh del token usa
    threading.Lock con doble check para que dos invocaciones simultáneas no gatillen
    dos POST /oauth/token.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, Optional

import requests
from dateutil import parser as dtparser
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from shared.secret_string import SecretString

logger = logging.getLogger(__name__)


class MercadoPagoError(RuntimeError):
    """Error envuelto con contexto útil para logging (sin exponer token)."""


class MercadoPagoAuthError(MercadoPagoError):
    """No se pudo obtener un access_token vía OAuth (credenciales mal o /oauth/token caído)."""


def _to_naive_utc(dt: datetime) -> datetime:
    """Normaliza a UTC naive. search_payments formatea con '...000Z' (UTC),
    así que toda la aritmética de ventanas se hace en UTC naive."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _parse_mp_dt(value: Any) -> Optional[datetime]:
    """Parsea una fecha de MP (ISO con offset, ej. '2026-05-27T07:03:35.000-04:00')
    a UTC naive. Devuelve None si no es parseable."""
    if not value:
        return None
    try:
        parsed = dtparser.parse(str(value))
    except (ValueError, TypeError, OverflowError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


class MercadoPagoClient:
    BASE_URL = "https://api.mercadopago.com"
    TOKEN_PATH = "/oauth/token"

    # Tras un 401 con token cacheado, invalidamos y reintentamos una sola vez.
    # Más reintentos no tienen sentido: si el secret está mal, falla igual.
    _MAX_401_RETRIES = 1

    def __init__(
        self,
        client_id: Optional[SecretString] = None,
        client_secret: Optional[SecretString] = None,
        access_token_override: Optional[SecretString] = None,
        timeout: int = 30,
        refresh_safety_factor: float = 0.8,
    ) -> None:
        """Construye el cliente.

        Args:
            client_id, client_secret: credenciales OAuth de la app MP. Requeridos
                salvo que se pase `access_token_override`.
            access_token_override: si se pasa, el cliente lo usa directo sin hacer
                OAuth. Pensado para dev local con tokens APP_USR del panel.
            timeout: timeout HTTP en segundos.
            refresh_safety_factor: fracción del expires_in tras la cual renovamos
                proactivamente (default 0.8 = renueva al 80%).
        """
        if access_token_override is None and (client_id is None or client_secret is None):
            raise ValueError(
                "MercadoPagoClient requiere (client_id, client_secret) o access_token_override"
            )
        if not 0.1 <= refresh_safety_factor < 1.0:
            raise ValueError("refresh_safety_factor debe estar en [0.1, 1.0)")

        self._client_id = client_id
        self._client_secret = client_secret
        self._access_token_override = access_token_override
        self.timeout = timeout
        self._refresh_safety_factor = refresh_safety_factor

        # Cache del token. _expires_at es time.monotonic() del momento en que
        # debemos refrescar (no del momento real de expiración del token).
        self._cached_token: Optional[str] = None
        self._expires_at: float = 0.0
        self._refresh_lock = threading.Lock()

        # Sesión con backoff exponencial para 429/5xx en endpoints de datos.
        # OJO: el endpoint /oauth/token NO usa esta sesión — lo hacemos con
        # requests.post directo porque el retry de Retry interactúa raro con POST.
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=frozenset(["GET"]),
        )
        self.session = requests.Session()
        self.session.mount("https://", HTTPAdapter(max_retries=retry))

    # ------------------------------------------------------------------
    # OAuth: cache + refresh
    # ------------------------------------------------------------------

    def _get_access_token(self) -> str:
        """Devuelve un access_token válido. Refresca si está expirado o por expirar."""
        if self._access_token_override is not None:
            return self._access_token_override.reveal()

        now = time.monotonic()
        if self._cached_token and now < self._expires_at:
            return self._cached_token

        with self._refresh_lock:
            # Double-check: otra invocación pudo haber refrescado mientras esperábamos.
            now = time.monotonic()
            if self._cached_token and now < self._expires_at:
                return self._cached_token
            self._refresh_token()
            return self._cached_token  # type: ignore[return-value]

    def _refresh_token(self) -> None:
        """Llama POST /oauth/token y actualiza el cache. Llamar dentro del lock."""
        assert self._client_id is not None and self._client_secret is not None
        url = f"{self.BASE_URL}{self.TOKEN_PATH}"
        body = {
            "client_id": self._client_id.reveal(),
            "client_secret": self._client_secret.reveal(),
            "grant_type": "client_credentials",
        }
        try:
            response = requests.post(
                url,
                json=body,
                headers={"Accept": "application/json"},
                timeout=self.timeout,
            )
        except requests.exceptions.RequestException as exc:
            raise MercadoPagoAuthError(
                f"POST /oauth/token falló: {type(exc).__name__}"
            ) from exc

        if response.status_code != 200:
            # No logueamos el body: puede contener client_id (no es ultra secreto pero igual).
            raise MercadoPagoAuthError(
                f"POST /oauth/token devolvió {response.status_code}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise MercadoPagoAuthError("POST /oauth/token devolvió JSON inválido") from exc

        token = data.get("access_token")
        expires_in = data.get("expires_in")
        if not isinstance(token, str) or not token:
            raise MercadoPagoAuthError("Respuesta de /oauth/token sin access_token")
        if not isinstance(expires_in, (int, float)) or expires_in <= 0:
            # Default conservador del doc MP: 21600s = 6h.
            expires_in = 21600

        self._cached_token = token
        self._expires_at = time.monotonic() + (expires_in * self._refresh_safety_factor)
        logger.info(
            "MP OAuth token refrescado (expires_in=%ss, refrescaremos en %.0fs)",
            int(expires_in),
            expires_in * self._refresh_safety_factor,
        )

    def _invalidate_token(self) -> None:
        """Fuerza un refresh en el próximo request. Usado tras 401."""
        self._cached_token = None
        self._expires_at = 0.0

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._get_access_token()}",
            "Accept": "application/json",
        }

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """GET con manejo de 401 (refresh + retry una vez) y errores envueltos."""
        url = f"{self.BASE_URL}{path}"
        attempts = 0
        while True:
            try:
                response = self.session.get(
                    url,
                    headers=self._headers(),
                    params=params,
                    timeout=self.timeout,
                )
            except requests.exceptions.RequestException as exc:
                raise MercadoPagoError(f"GET {path} falló: {type(exc).__name__}") from exc

            # 401 con override no se reintenta: si el token estático está mal, no hay refresh posible.
            if (
                response.status_code == 401
                and self._access_token_override is None
                and attempts < self._MAX_401_RETRIES
            ):
                logger.warning("GET %s devolvió 401; invalidando token y reintentando", path)
                self._invalidate_token()
                attempts += 1
                continue

            try:
                response.raise_for_status()
            except requests.exceptions.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else "?"
                raise MercadoPagoError(f"GET {path} falló con status {status}") from exc

            try:
                return response.json()
            except ValueError as exc:
                raise MercadoPagoError(f"GET {path} devolvió JSON inválido") from exc

    # ------------------------------------------------------------------
    # Endpoints públicos
    # ------------------------------------------------------------------

    def get_payment(self, payment_id: str) -> Dict[str, Any]:
        """Devuelve el payment completo dado su ID. Usado tras recibir un webhook."""
        if not payment_id:
            raise ValueError("payment_id vacío")
        return self._get(f"/v1/payments/{payment_id}")

    # MP capea la paginación: offset <= 10_000 y limit <= 50 (máx 10_000
    # resultados por ventana de búsqueda). Para traer más hay que partir el
    # rango de fechas. Dejamos margen bajo el tope para evitar edge cases.
    OFFSET_CAP = 9_900

    def search_payments(
        self,
        begin_date: datetime,
        end_date: datetime,
        limit: int = 50,
        offset: int = 0,
        status: Optional[str] = None,
        range_field: str = "date_last_updated",
        sort: Optional[str] = None,
        criteria: str = "desc",
    ) -> Dict[str, Any]:
        """Búsqueda paginada por rango de fechas.

        El response incluye `paging.total` (total de matches) y `results` (página
        actual). El caller itera incrementando offset hasta cubrir total.

        Args:
            begin_date, end_date: rango ISO 8601. Deben ser UTC (naive o aware);
                se formatean como "...000Z".
            limit: 1..50 (MP cap a 50 según doc Rapanui sección 3.1).
            offset: desplazamiento (MP cap a 10_000).
            status: opcional, filtrar por status (approved/pending/etc).
            range_field: campo del rango ("date_last_updated" default, o
                "date_created" para enumerar histórico, etc.).
            sort: campo de ordenamiento (default = range_field).
            criteria: "asc" | "desc" (default "desc").
        """
        if not 1 <= limit <= 50:
            raise ValueError("limit fuera de rango [1, 50]")
        if offset < 0:
            raise ValueError("offset negativo")

        params: Dict[str, Any] = {
            "sort": sort or range_field,
            "criteria": criteria,
            "range": range_field,
            "begin_date": begin_date.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "end_date": end_date.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "limit": limit,
            "offset": offset,
        }
        if status:
            params["status"] = status
        return self._get("/v1/payments/search", params=params)

    def iter_all_payments(
        self,
        begin: datetime,
        end: datetime,
        range_field: str = "date_created",
        status: Optional[str] = None,
        page_size: int = 50,
        page_delay_seconds: float = 0.0,
    ) -> Iterator[Dict[str, Any]]:
        """Itera TODOS los pagos en [begin, end], esquivando el cap de offset de MP.

        MP no deja paginar más allá de offset 10_000 por consulta. Para traer un
        universo más grande (ej. 95_000 pagos en un mes), partimos el rango: cuando
        el offset llega a `OFFSET_CAP`, avanzamos `begin` a la última fecha vista y
        reseteamos el offset. Ordenamos ascendente por `range_field` para que el
        avance sea monotónico, y deduplicamos por `payment_id` en los bordes de
        ventana (la fecha de corte se re-incluye al ser >=).

        Yields cada payment completo (el objeto que devuelve /payments/search).
        """
        cursor = _to_naive_utc(begin)
        end = _to_naive_utc(end)
        seen: set = set()

        while cursor < end:
            offset = 0
            last_dt: Optional[datetime] = None
            window_yielded = 0

            while offset <= self.OFFSET_CAP:
                resp = self.search_payments(
                    begin_date=cursor, end_date=end,
                    limit=page_size, offset=offset,
                    status=status, range_field=range_field,
                    sort=range_field, criteria="asc",
                )
                results = resp.get("results") or []
                total = (resp.get("paging") or {}).get("total")
                if not results:
                    return

                for payment in results:
                    pid = payment.get("id")
                    if pid is not None:
                        pid_str = str(pid)
                        if pid_str not in seen:
                            seen.add(pid_str)
                            window_yielded += 1
                            yield payment
                    raw = payment.get(range_field)
                    parsed = _parse_mp_dt(raw)
                    if parsed is not None:
                        last_dt = parsed

                offset += len(results)
                if (isinstance(total, int) and offset >= total) or len(results) < page_size:
                    return  # ventana [cursor, end] drenada por completo bajo el cap
                if page_delay_seconds > 0:
                    time.sleep(page_delay_seconds)

            # Alcanzamos el cap: hay más de OFFSET_CAP matches en [cursor, end].
            # Avanzamos la ventana al último date visto y seguimos.
            if last_dt is None or last_dt <= cursor:
                logger.warning(
                    "iter_all_payments: cursor no avanzó desde %s (last_dt=%s); "
                    "corte para evitar loop infinito", cursor, last_dt,
                )
                return
            logger.info(
                "iter_all_payments: ventana llena en %s; avanzo cursor a %s",
                cursor.isoformat(), last_dt.isoformat(),
            )
            cursor = last_dt
