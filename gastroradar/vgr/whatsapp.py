"""Cliente minimo de WhatsApp Cloud API.

Un HTTP 200 solo significa ACEPTADO. Enviado, entregado, leido o fallido llegan
despues por el webhook de estados.
"""
import requests

# Codigos de Meta que indican limite temporal: el mensaje no se acepto.
RATE_CODES = {4, 80007, 130429, 131048, 131056}


class SendOutcome:
    def __init__(self, kind, wamid="", detail=""):
        self.kind, self.wamid, self.detail = kind, wamid, detail  # accepted|retry|rejected|uncertain


class WhatsApp:
    def __init__(self, cfg, http=None):
        self.cfg, self.http = cfg, http or requests

    def _base(self):
        return f"https://graph.facebook.com/{self.cfg.graph_version}/{self.cfg.phone_number_id}"

    def _headers(self):
        return {"Authorization": f"Bearer {self.cfg.meta_token}"}

    def send(self, payload: dict) -> SendOutcome:
        try:
            resp = self.http.post(self._base() + "/messages", headers=self._headers(),
                                  json={"messaging_product": "whatsapp", **payload}, timeout=(5, 15))
        except requests.exceptions.ConnectTimeout:
            return SendOutcome("retry", detail="sin_conexion")
        except requests.exceptions.RequestException as exc:
            # La solicitud pudo llegar: reintentar podria duplicar el mensaje.
            return SendOutcome("uncertain", detail=type(exc).__name__)
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        if resp.status_code in (200, 201):
            try:
                return SendOutcome("accepted", wamid=body["messages"][0]["id"])
            except (KeyError, IndexError, TypeError):
                return SendOutcome("uncertain", detail="200_sin_wamid")
        error = body.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        detail = f"http_{resp.status_code}_code_{code}"
        if resp.status_code == 429 or code in RATE_CODES:
            return SendOutcome("retry", detail=detail)
        if resp.status_code >= 500:
            return SendOutcome("uncertain", detail=detail)
        return SendOutcome("rejected", detail=detail)

    def check_number(self):
        """Solo lectura: valida el token y devuelve el numero conectado."""
        try:
            resp = self.http.get(self._base() + "?fields=display_phone_number", headers=self._headers(),
                                 timeout=(5, 10))
            if resp.status_code != 200:
                return {"token_ok": False, "detalle": f"http_{resp.status_code}", "numero": ""}
            return {"token_ok": True, "detalle": "", "numero": resp.json().get("display_phone_number", "")}
        except Exception as exc:
            return {"token_ok": False, "detalle": type(exc).__name__, "numero": ""}
