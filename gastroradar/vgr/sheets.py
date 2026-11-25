"""Espejo de prospectos en Google Sheets por REST (sin googleapiclient.discovery,
que en este ecosistema ya se asocio a consumo creciente de memoria).

Redis es el registro principal; la hoja se actualiza por ID unico (upsert)."""
import json
from urllib.parse import quote

HEADERS = ["ID único", "Fecha y hora", "Nombre", "WhatsApp", "Negocio", "Municipio",
           "Servicio de interés", "Origen", "Consentimiento", "Estado", "Última actualización", "Historial"]
SCOPE = "https://www.googleapis.com/auth/spreadsheets"


class SheetError(Exception):
    pass


class Sheets:
    def __init__(self, cfg, session=None):
        self.cfg, self._session, self._header_ok = cfg, session, False

    @property
    def configured(self):
        return bool(self.cfg.sheet_id and (self.cfg.google_credentials or self._session))

    def _http(self):
        if self._session is None:
            from google.auth.transport.requests import AuthorizedSession
            from google.oauth2.service_account import Credentials
            creds = Credentials.from_service_account_info(json.loads(self.cfg.google_credentials),
                                                          scopes=[SCOPE])
            self._session = AuthorizedSession(creds)
        return self._session

    def _call(self, method, a1, suffix="", **kwargs):
        tab = self.cfg.sheet_tab.replace("'", "''")
        rng = quote(f"'{tab}'!{a1}", safe="")
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{self.cfg.sheet_id}/values/{rng}{suffix}"
        resp = self._http().request(method, url, timeout=10, **kwargs)
        if resp.status_code != 200:
            raise SheetError(f"http_{resp.status_code}")
        return resp.json()

    def upsert(self, row: list):
        if not self.configured:
            raise SheetError("sin_configurar")
        if not self._header_ok:
            if not self._call("GET", "A1:L1").get("values"):
                self._call("PUT", "A1:L1", params={"valueInputOption": "RAW"}, json={"values": [HEADERS]})
            self._header_ok = True
        ids = self._call("GET", "A:A").get("values", [])
        index = next((i for i, v in enumerate(ids, 1) if v and v[0] == row[0]), None)
        # RAW: un nombre como "=IMPORTXML(...)" se guarda como texto, no como formula.
        if index:
            self._call("PUT", f"A{index}:L{index}", params={"valueInputOption": "RAW"}, json={"values": [row]})
        else:
            self._call("POST", "A:L", ":append",
                       params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
                       json={"values": [row]})
