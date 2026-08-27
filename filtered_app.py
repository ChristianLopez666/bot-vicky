"""Wrapper de arranque que reduce exclusivamente la persistencia en Sheets.

El runtime, rutas, funnels, envíos, estados y objeto Flask siguen siendo los de
app.py. Solo se sustituye la función de bitácora después de importar el módulo.
"""

import app as _core
from sheet_log_policy import should_persist_log

_original_log = _core._log


def _filtered_log(phone, nombre, msg, tipo, origen, resultado="", error="", mid=""):
    if not should_persist_log(tipo, origen, resultado, error):
        return
    return _original_log(phone, nombre, msg, tipo, origen, resultado, error, mid)


_core._log = _filtered_log
app = _core.app
