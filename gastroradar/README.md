# Vicky GastroRadar v1

Asistente **receptivo** de WhatsApp para GastroRadar. Sin Boardroom ni procesos financieros. No envía campañas.

**Estado: NO APTO PARA DESPLIEGUE.** Ver [informe](docs/INFORME_AUDITORIA.md).

## Cómo funciona

1. `POST /webhook` valida la firma de Meta, descarta lo que no sea del `phone_number_id` propio y guarda cada mensaje en Redis junto con su marca de duplicado, en una sola transacción. Si Redis falla responde 503 y Meta reintenta.
2. Hilos del mismo proceso atienden la cola. Hay un carril por usuario, así que sus mensajes van en orden y de uno en uno.
3. Estado de conversación, prospecto, consentimiento y respuesta pendiente se confirman juntos.
4. Cada envío recorre `pendiente → enviando → aceptado → sent → delivered → read`, o termina en `fallido`, `incierto`, `expirado` o `cancelado`. **HTTP 200 de Meta solo es "aceptado".**
5. Un envío `incierto` no se reintenta solo, para no duplicar mensajes.

Cola dentro de Redis y sin servicio "worker" aparte: evita pagar otro servicio. Si el volumen crece, los hilos pueden moverse a otro proceso sin cambiar el formato de la cola.

## IA

Apagada por defecto (`AI_ENABLED=false`). Cuando se enciende, solo se usa en texto libre que las reglas no resuelven:

- Precios, planes, banners, terminal, registro y soporte salen **siempre** de `vgr/knowledge.py`.
- El modelo no cambia consentimiento, no crea prospectos y no ejecuta acciones.
- Su salida se rechaza si trae importes, otros dominios, teléfonos, temas comerciales o dice haber hecho algo.
- Recibe texto sin números largos ni correos. **Los nombres y demás texto libre sí viajan a OpenAI.**
- Límites configurables: longitud, tokens, llamadas por usuario y globales al día. Tras 3 fallos seguidos se pausa 5 minutos.

Límite honesto: los filtros no garantizan que una frase libre del modelo sea verdadera.

## Probar

```
pip install -r requirements-dev.txt
python -m pytest tests -q
```

Las pruebas usan datos ficticios, `fakeredis` y dobles de Meta, OpenAI y Sheets, y bloquean la red.

## Operación

- `GET /health`: el proceso vive. `GET /ready`: configuración, Redis, hilos, atraso de cola y asesor (503 si algo crítico falla). `/ready` no comprueba el token de Meta; eso lo hace el diagnóstico.
- Rutas con cabecera `X-Admin-Token` (24 caracteres o más):
  - `GET /admin/diagnostico`: token de Meta, número conectado, persistencia y política de memoria de Redis.
  - `GET /admin/prospectos`: recupera prospectos aunque Sheets falle.
  - `GET /admin/fallidos`: trabajos agotados y problemas de entrega.
  - `POST /admin/reintentar`: reencola hojas y mensajes entrantes; nunca envíos.
  - `POST /admin/prospectos/<id>/avisar`: reenvía un aviso tras revisarlo una persona.
- El número del asesor **nunca recibe respuestas del bot**. Para probar como usuario usa otro número.
- `GASTRORADAR_ENABLED=false`: responde 200 y **descarta** lo que llegue, incluidas las bajas. Úsalo solo antes del corte o en una emergencia.

## Datos que guarda Redis

Sesión 7 días; envíos 90 días (incluyen teléfono y texto). Consentimientos, prospectos, historial de avisos y trabajos fallidos **no vencen**. Falta definir una política de retención y un procedimiento de borrado.
