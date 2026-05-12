"""Cliente HTTP mínimo para la API de Mercado Pago.

Solo expone los endpoints que necesita la Function:
    - GET /v1/payments/{id}     → hidratar un payment a partir de un webhook.
    - GET /v1/payments/search   → búsqueda por rango de fechas (útil para
                                   backfill manual desde la carga inicial).

Respecto al cliente del servicio monolítico:
    - No incluye el bucle de paginación ni el sleep de 150 ms (la Function es
      event-driven, procesa UN payment por invocación).
    - Usa SecretString para el access_token.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from shared.secret_string import SecretString

logger = logging.getLogger(__name__)


class MercadoPagoError(RuntimeError):
    """Error envuelto con contexto útil para logging (sin exponer token)."""


class MercadoPagoClient:
    BASE_URL = "https://api.mercadopago.com"

    def __init__(self, access_token: SecretString, timeout: int = 30) -> None:
        if not access_token:
            raise ValueError("MercadoPagoClient requiere un access_token no vacío")
        self._access_token = access_token
        self.timeout = timeout

        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=frozenset(["GET"]),
        )
        self.session = requests.Session()
        self.session.mount("https://", HTTPAdapter(max_retries=retry))

    # ------------------------------------------------------------------
    # Helpers privados
    # ------------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        # Solo acá revelamos el token; nunca lo guardamos plano en self.session.headers
        # porque eso lo filtraría al imprimir la sesión o inspeccionar el cliente.
        return {
            "Authorization": f"Bearer {self._access_token.reveal()}",
            "Accept": "application/json",
        }

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.BASE_URL}{path}"
        try:
            response = self.session.get(
                url,
                headers=self._headers(),
                params=params,
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as exc:
            # Loguear solo status + path, nunca el response.text completo (puede
            # devolver fragmentos del request con headers).
            status = exc.response.status_code if exc.response is not None else "?"
            raise MercadoPagoError(f"GET {path} falló con status {status}") from exc
        except requests.exceptions.RequestException as exc:
            raise MercadoPagoError(f"GET {path} falló: {type(exc).__name__}") from exc

    # ------------------------------------------------------------------
    # Endpoints públicos
    # ------------------------------------------------------------------

    def get_payment(self, payment_id: str) -> Dict[str, Any]:
        """Devuelve el payment completo dado su ID. Usado tras recibir un webhook."""
        if not payment_id:
            raise ValueError("payment_id vacío")
        return self._get(f"/v1/payments/{payment_id}")

    def search_payments(
        self,
        begin_date: datetime,
        end_date: datetime,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Búsqueda por rango de fechas. Útil para backfill manual."""
        params = {
            "sort": "date_created",
            "criteria": "desc",
            "range": "date_created",
            "begin_date": begin_date.strftime("%Y-%m-%dT%H:%M:%S"),
            "end_date": end_date.strftime("%Y-%m-%dT%H:%M:%S"),
            "limit": limit,
            "offset": offset,
        }
        return self._get("/v1/payments/search", params=params)
