#!/bin/bash
# Uso: run.sh <python>. Corre dentro de un espacio de red sin salida.
PY="$1"
if curl -s --max-time 5 https://graph.facebook.com >/dev/null; then echo "hay red: no se continúa"; exit 1; fi
HERE="$(cd "$(dirname "$0")" && pwd)"
for spec in "antes:$ANTES_DIR" "despues:$DESPUES_DIR"; do
  label="${spec%%:*}"; dir="${spec#*:}"
  for rep in 1 2 3; do
    # Render arranca con --preload (en sus registros la app se importa antes de "Starting gunicorn").
    APP_DIR="$dir" "$PY" -m gunicorn probe_wsgi:app --chdir "$HERE" --preload --bind 127.0.0.1:8000 \
      --timeout 180 >"gunicorn_${label}_$rep.log" 2>&1 &
    pid=$!
    "$PY" "$HERE/load.py" "$pid" "$label-$rep"
    kill "$pid"; wait "$pid" 2>/dev/null
    echo "  errores en el registro: $(grep -cE 'Error en Sheets|Error en reporte|Traceback' "gunicorn_${label}_$rep.log")  envios_a_Meta: $(grep -c META_POST "gunicorn_${label}_$rep.log")"
  done
done
