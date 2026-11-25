#!/bin/bash
# Arranque con gunicorn dentro de un espacio de red SIN salida. Datos ficticios.
set -u
PY="$1"
fail=0
check() { if [ "$2" = "$3" ]; then echo "OK    $1: $3"; else echo "FALLA $1: esperado $2, obtenido $3"; fail=1; fi; }

if curl -s --max-time 5 https://graph.facebook.com >/dev/null; then
  echo "FALLA: hay salida a Internet; no se continúa"; exit 1
fi
echo "OK    sin salida a Internet (graph.facebook.com inalcanzable)"

export META_TOKEN=ficticio WABA_PHONE_ID=111 VERIFY_TOKEN=ficticio META_APP_SECRET=ficticio \
       ADVISOR_NUMBER=5215550000099

for entry in app:app filtered_app:app; do
  echo "===== gunicorn $entry"
  log="boot_${entry%%:*}.log"
  "$PY" -m gunicorn "$entry" --bind 127.0.0.1:8000 >"$log" 2>&1 &
  pid=$!
  for _ in $(seq 1 60); do curl -s --max-time 2 127.0.0.1:8000/health >/dev/null && break; sleep 0.5; done
  check "/health" '{"sheets":false,"status":"ok"}' "$(curl -s --max-time 5 127.0.0.1:8000/health | tr -d ' \n')"
  check "verificación de webhook" "reto123" \
    "$(curl -s --max-time 5 '127.0.0.1:8000/webhook?hub.mode=subscribe&hub.verify_token=ficticio&hub.challenge=reto123')"
  body='{"entry":[{"changes":[{"value":{"metadata":{"phone_number_id":"111"},"messages":[{"from":"5215550000001","id":"wamid.PRUEBA1","type":"text","text":{"body":"hola"}}]}}]}]}'
  sig=$(printf '%s' "$body" | openssl dgst -sha256 -hmac ficticio | awk '{print $NF}')
  check "webhook firmado" "200" \
    "$(curl -s -o /dev/null -w '%{http_code}' --max-time 40 -H 'Content-Type: application/json' -H "X-Hub-Signature-256: sha256=$sig" --data "$body" 127.0.0.1:8000/webhook)"
  check "firma inválida" "403" \
    "$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -H 'X-Hub-Signature-256: sha256=00' --data "$body" 127.0.0.1:8000/webhook)"
  sleep 5
  if kill -0 "$pid" 2>/dev/null; then echo "OK    el proceso sigue vivo"; else echo "FALLA el proceso murió"; fail=1; fi
  kill "$pid"; wait "$pid" 2>/dev/null
  echo "--- registro de $entry"
  cat "$log"
done
exit $fail
