# Cierre de la auditoría de Work (issue #24) — 25-nov-2026

**Dictamen: NO APTO PARA SUSTITUIR PRODUCCIÓN.** VGR-01 está corregido en simulación. La durabilidad no es viable sin gasto con el diseño actual.

**Alcance:** el `app.py` v0.1 nunca se recibió. Esta versión se **construyó de nuevo**. Es implementación y autoauditoría propias, no una auditoría de aquel archivo.

## Código preparado y versión publicada

| | |
|---|---|
| Rama de trabajo | `feat/vicky-gastroradar-v1`, base `main` = `dc6f64d` |
| Servido en Render (leído en Render, no deducido de `main`) | `dc6f64d`, el bot financiero anterior. Auto-Deploy desde `main` activado |
| Esta versión | **No está desplegada** |

## VGR-01 — negativa de contacto: corregido (simulación)

La secuencia de Work (ASESOR → SÍ → "No quiero un asesor") se reprodujo y ya no genera aviso.

**Qué hace una negativa ahora:** retira la solicitud, deja el prospecto en `cancelado` / `No contactar`, cancela el seguimiento y los avisos en cola, y exige un SÍ nuevo para volver a contactar. No pide escribir BAJA. No borra registros.

**Alcance de la cancelación**

| Mensaje | Se cancela |
|---|---|
| Negativa general ("no quiero un asesor") | La solicitud en curso, y las que **aún no llegaron** al asesor (enviarlas contradiría la negativa). Las ya enviadas de otros servicios no cambian, y se le dice al usuario |
| Termina nombrando un servicio ("… para la terminal") | Solo ese |
| "Cancelar" y equivalentes | Solo la solicitud en curso |
| "No gracias", "mejor no", "salir" y similares | Solo mientras se piden datos; como despedida no cancelan nada |
| BAJA | Todo, como antes |

Si el aviso **ya salió**, Vicky dice que no puede deshacerlo y envía al asesor una indicación de no contactar, sin asegurar que la lea a tiempo. Si el envío quedó incierto, dice que pudo haberse enviado.

**Fallos relacionados que se encontraron y corrigieron en la misma ronda**

- "Cancelar" en las preguntas pedía escribir BAJA y dejaba el aviso programado.
- Un aviso en cola de una autorización anterior podía salir tras autorizar de nuevo, y un seguimiento viejo podía actuar sobre la nueva.
- Una autorización renovada no programaba su seguimiento.
- Una negativa podía cruzarse con el envío: ahora el aviso espera mientras el usuario tenga un mensaje en proceso o sin procesar.
- El límite de frecuencia y la antigüedad mayor a 24 h podían descartar una negativa.
- "No me llame" (trato de usted) y frases como "siempre no" o "cancélalo" no se reconocían.
- "No, quiero un asesor" se leía como negativa.
- Mencionar otro tema ("… por el precio") podía desviar la cancelación.

**Evidencia:** `tests/test_cancelacion.py`. Con el código auditado (`6b40f68`) esos casos dan **85 fallos y 13 aciertos**; con la corrección pasan todos. Total en [RESULTADOS_PRUEBAS.txt](RESULTADOS_PRUEBAS.txt).

**Límite:** el reconocimiento es por frases. Una negativa dicha de otra forma puede no detectarse, y tras 30 minutos saldría el aviso "incompleto". Si ese riesgo no es aceptable, la opción es avisar solo cuando la persona termina de dar sus datos (decisión de producto, no aplicada).

## VGR-02 — las 24 horas: pendiente, causa no determinada

Falta un dato indispensable: **qué síntoma se observó** (el bot no contesta, el asesor no recibe avisos, o el bot olvida la conversación) y en qué fecha. Cada uno apunta a una causa distinta.

Fuente: servicio, eventos, métricas y registros de Render en solo lectura. Los registros disponibles empiezan el 18-nov cerca de las 17:00 UTC (7 días). Las métricas se revisaron desde el 27-oct.

| Hipótesis | Evidencia | Estado |
|---|---|---|
| Expiración de estado | El código publicado borra el estado de conversación a las 24 h sin actividad (`STATE_TTL`; leerlo renueva el plazo). Una persona que vuelve después empieza de cero | **Mecanismo confirmado en código.** No se sabe si es el síntoma |
| Ventana de WhatsApp (avisos al asesor) | Hay un diagnóstico previo (30-jul, anterior a esta auditoría): fuera de la ventana el aviso en texto libre no se entregaba. El código publicado ya intenta plantilla. En los 7 días: sin errores 131047 y **ningún aviso al asesor registrado**, así que esa ruta no se ejercitó | Sin comprobar hoy. No se leyó si `ADVISOR_TEMPLATE_NAME` sigue configurada |
| Token | El 25-nov Meta aceptó respuestas y reportó entrega. Sin errores `OAuthException` en la búsqueda | No explica el comportamiento de hoy. No descarta un vencimiento anterior; tipo de token no verificado |
| Suspensión | Servicio no suspendido, 1 instancia en cada intervalo de 6 h desde el 27-oct, con peticiones de monitor cada pocos minutos | Sin indicio. Cortes breves no se ven con esa resolución |
| Reinicios | El 19-nov 09:49 UTC un proceso recibió SIGKILL con memoria cercana a 528 de 537 MB. Hoy, tras dos mensajes, la memoria pasó de ~291 a ~478 MB y se mantuvo | **Riesgo comprobado.** No se probó que cause el síntoma |
| Pérdida de almacenamiento | El único Key Value de la cuenta es gratuito y sin persistencia. Los registros no permiten saber si el bot publicado lo usa | No determinado |
| Webhook | Sin entregas de Meta en los registros desde el 18-nov hasta el 25-nov 17:20 UTC; ese día dos mensajes entraron, se respondieron y WhatsApp reportó entrega | Funciona hoy. No se sabe si antes nadie escribió |

**Cómo cerrar el diagnóstico (sin gasto ni cambios):**

1. Indicar el síntoma y una fecha y hora.
2. En Render → servicio del bot → *Environment*, confirmar solo si **existen** los nombres `KV_URL` (o `REDIS_URL`) y `ADVISOR_TEMPLATE_NAME`. No copiar valores.
3. La próxima vez que falle, revisarlo dentro de los 7 días, antes de que se borren los registros.

**Mensajes programados:** no se presentan como solución. Sin causa demostrada no hay base para ello; tampoco renuevan un token ni recuperan estado.

## VGR-03 — costo: bloqueado con el diseño actual

**Recursos vistos en la cuenta (25-nov):** servicio web del bot (plan Starter, 1 instancia), servicio web de SECOM (Starter), un Key Value gratuito, ningún Postgres; Boardroom y un cron suspendidos (no se tocan). No se revisó facturación.

**Condiciones oficiales de Render** ([Key Value](https://render.com/docs/key-value), [plan gratuito](https://render.com/docs/free), [precios](https://render.com/pricing)):

- Key Value gratuito: 25 MB, sin persistencia, **al reiniciarse se pierde todo**, Render puede reiniciarlo en cualquier momento y solo se permite uno por cuenta.
- Key Value de pago más barato: **US$10/mes** (256 MB), con persistencia en disco en planes de pago. La página no detalla los modos de ese tamaño.
- Disco persistente: **US$0.25 por GB al mes**; desactiva los despliegues sin interrupción.
- Postgres gratuito: caduca a los 30 días.
- Los servicios web gratuitos se suspenden tras 15 minutos sin tráfico.

**Conclusión:** la versión nueva guarda en Redis las bajas, las autorizaciones, los prospectos, la cola y los duplicados. Con lo que ya existe **no hay forma de darle durabilidad a costo cero**: perdería bajas y prospectos. Además, el Key Value actual usa `allkeys_lru`, que permite desalojar claves al llenarse. Es un bloqueo. No se contrató ni cambió nada.

| Alternativa | Gasto adicional | Qué implica |
|---|---|---|
| A. Dejar publicado el bot actual | 0 | GastroRadar no entra en WhatsApp |
| B. Modo solo informativo: sin captar prospectos ni avisar al asesor; remite al sitio y al correo | 0 en infraestructura | **No construido.** Como nunca inicia contacto, no hay autorizaciones que conservar. Aún necesitaría cola en Redis (perderla tendría menos impacto, no nulo); falta confirmar quién usa el Key Value actual |
| C. Disco en el servicio actual y guardar en SQLite | US$0.25/mes por GB | **No construido.** Cambia la capa de almacenamiento y hay interrupción en cada despliegue |
| D. Key Value de pago | US$10/mes | Funciona con el código actual, pendiente de ensayo real |

Guardar todo en Google Sheets no se evaluó a fondo: exigiría rediseñar la cola y los duplicados. No se evaluaron proveedores externos.

**WhatsApp** ([precios oficiales](https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing), vigentes desde el 1-oct-2026; aplican igual al bot actual): las respuestas de texto dentro de la ventana se cobran por mensaje entregado a partir del 1,001 de cada mes **por número**; las plantillas de utilidad se cobran siempre; desde anuncios de clic a WhatsApp puede abrirse una ventana sin cargo de hasta 7 días. Sin método de pago, lo que exceda la cuota gratuita no se entrega. **No se leyeron las tarifas en pesos o dólares para México.** Las cifras por mensaje de la versión anterior del informe venían de terceros y quedan retiradas. Hay que confirmar la categoría real de la plantilla del asesor: una nota del 1-ago dice que la anterior se aprobó como *marketing*.

**IA:** apagada; costo 0. **Memoria de la versión nueva en Starter:** no medida.

## Estado de cada hallazgo

| Hallazgo | Estado |
|---|---|
| VGR-01 negativa de contacto | **Resuelto en simulación**; sin prueba con proveedor |
| VGR-02 24 horas | **Pendiente**: falta síntoma, fecha y dos nombres de variables |
| VGR-03 costo y durabilidad | **Bloqueado**: requiere elegir alternativa |
| VGR-04 ensayo real y corte | **Pendiente**: ver plan; requiere autorización |
| VGR-05 retención, borrado, alerta si falla el aviso | **Pendiente**. IA sigue apagada |
| `/ready` en 200 sin plantilla ni Sheets | Sin cambio: un 200 **no** prueba entrega. Usar `/admin/diagnostico` y un aviso `delivered` |

## Niveles de evidencia

| Nivel | Qué hay |
|---|---|
| Simulación (`fakeredis`; dobles de Meta, Sheets y OpenAI) | Toda la versión nueva, incluida la corrección |
| CI en Linux | Suite y arranque de gunicorn sin credenciales |
| Proveedor real | **Nada** de la versión nueva |
| Producción | Solo observaciones de lectura del bot **anterior** |

## Plan mínimo de pruebas reales (ninguna ejecutada)

Cada etapa requiere autorización antes de empezar.

| Etapa | Qué prueba | Recursos | Mensajes | Costo posible |
|---|---|---|---|---|
| 0 | Cola y transacciones con un Valkey real | Contenedor de Valkey en el CI existente (cambio pequeño, no hecho) | 0 | Confirmar que el CI sigue sin cargo |
| 1 | WhatsApp, firma, avisos, negativa antes y después del envío, nueva autorización, BAJA, mensaje tras 24 h | App y número de prueba de Meta **separados** de la app de producción; bot y Valkey en un equipo local con túnel temporal; hoja de prueba; plantilla de prueba | ~32 entrantes, ~32 respuestas, ~5 plantillas al asesor | Render 0. Si Meta factura el número usado: respuestas dentro de las 1,000 gratis si no se han consumido, y ~5 plantillas a la tarifa oficial de México, **por consultar antes**. No se verificó si el número de prueba genera cargos |
| 2 | Reinicio y pérdida del almacén reales, restauración en Render | Solo tras elegir la alternativa de almacenamiento | ~10 | Según la alternativa |

La etapa 1 no prueba el almacén ni el despliegue de Render. No cambia el webhook compartido con SECOM.

## Reversión

- Hoy: nada fusionado ni desplegado. Cerrar el PR borrador basta; `main` no cambia.
- **Cuidado al fusionar:** Render despliega solo desde `main`. Aunque el cambio solo agrega `gastroradar/` y un workflow, fusionar **redespliega y reinicia el bot publicado**. No fusionar sin desactivar antes Auto-Deploy.
- Si ya se hubiera fusionado: `git revert -m 1 <merge>` en `main` (también redespliega).
- Corte y restauración del servicio: [DESPLIEGUE_Y_RESTAURACION.md](DESPLIEGUE_Y_RESTAURACION.md).
