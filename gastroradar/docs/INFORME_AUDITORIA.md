# Informe de auditoría — Vicky GastroRadar v1 (25-nov-2026)

## Dictamen: NO APTO PARA DESPLIEGUE

El código pasa sus pruebas locales con servicios simulados. No se ha probado contra WhatsApp, un Redis real, Google Sheets, OpenAI ni Render.

El `app.py` v0.1 no se recibió. Esta versión se **escribió desde cero**: no es una auditoría de la v0.1. La auditoría es propia; falta la revisión independiente prevista.

## Etapa A — Diagnóstico (solo lectura)

| Punto | Resultado |
|---|---|
| Commit en GitHub | `main` = `dc6f64d` (18-sep). PR 16 abierto. Repositorio **público** |
| Versión en Render | **No verificada** |
| Salud actual | `/health` 200 una vez, `sheets: true` |
| Arranque según el repositorio | `filtered_app:app` |
| Número conectado | **No verificado** |
| Aviso al asesor anterior | Texto libre con respaldo de plantilla (`ADVISOR_TEMPLATE_NAME`) |
| Sheets / Redis reales | **No verificados** |
| Respaldo | Paquete git creado, verificado y restaurado localmente |
| Suite anterior | 567 pasan, 19 fallan; GitHub reporta lo mismo en `main` |
| Historial en busca de secretos | **No revisado** |

## Etapa E — Las 24 horas

**Causa real: no determinada.** No se leyeron los registros de Render. Hipótesis a contrastar:

| Hipótesis | Evidencia disponible | Cómo confirmarla |
|---|---|---|
| Pérdida de estado a las 24 h | El código anterior usa `STATE_TTL` = 24 h | Comparar hora del último mensaje con el fallo |
| Reinicios borran la memoria | Nota del 22-sep sobre reinicios con 512 MB; sin Redis el bot anterior usa memoria | Eventos de Render y arranque "Estado en memoria" |
| Ventana de 24 h de WhatsApp | Fuera de ella no se entrega texto libre; afecta avisos al asesor | Estados `failed`, código 131047 |
| Token temporal o vencido | Nota del 10-ago: un token del Graph API Explorer tiró el bot | Tipo y vencimiento del token en Meta |
| Suspensión por inactividad | Solo aplica a plan gratuito; plan no verificado | Plan del servicio |
| Webhook u OpenAI | El webhook anterior responde 200 aun si falla el proceso, así que Meta no reintenta | Registros "webhook POST" |

**Mensajes programados:** no sirven como solución. Un mensaje a la hora programada puede despertar un servicio y abre la ventana solo del número que lo envía. No renueva tokens ni recupera estado. Si se envía desde el número del asesor, la versión nueva no lo contesta. Para vigilar conviene un monitor externo sobre `/ready` más un estado de entrega revisado.

La versión nueva usa plantilla para el asesor, no responde mensajes con más de 24 h y registra cada fallo de entrega. No envía campañas.

## Errores encontrados en el ciclo adversarial y corregidos

Sobre la primera versión escrita hoy:

1. **Crítico:** "no quiero que me contacten" iniciaba el contacto en lugar de revocar.
2. No se reconocían otras frases de baja ("dejen de escribirme", "ya no me manden mensajes", "eliminen mi número").
3. "No quiero un asesor" iniciaba el contacto.
4. Frases como "quiero publicidad" o "no me interesa" se guardaban como nombre.
5. El usuario quedaba atrapado en las preguntas; no podía volver al menú.
6. Si el prospecto completaba sus datos después del aviso "incompleto", el asesor no los recibía.
7. El modelo podía afirmar cosas de planes, banners o terminal sin cifras.
8. Una consulta de soporte podía quedar respondida por la IA en vez del texto fijo.
9. El reintento manual esperaba la pausa del carril (hasta 1 h).

Detectado al revisar el diseño, antes de las pruebas: el resultado de un aviso podía sobrescribir una baja simultánea. Ahora el estado del aviso se guarda aparte y el prospecto solo se escribe en el carril de su usuario.

## Resultados

Ver [RESULTADOS_PRUEBAS.txt](RESULTADOS_PRUEBAS.txt). Las pruebas `test_01` a `test_24` corresponden a las 24 obligatorias. Límites:

- **11, 12, 14:** concurrencia, reinicio y caída de Redis simulados con `fakeredis`, no con un Redis real.
- **13, 15, 16, 17:** dobles de OpenAI, Sheets y Meta.
- **20:** simula entrega atrasada; no una suspensión real de Render.
- **24:** comprueba que los archivos anteriores no cambiaron y que la app vieja importa y responde `/health`. No es una restauración en Render.
- `gunicorn` no corre en Windows. En GitHub Actions (Linux, Python 3.13, commit `fd96700`, ejecución 45661024930) arrancó sin credenciales y `/health` respondió bien: 104 pruebas pasan y 1 se omite. Es la única con omisión prevista allí: `test_24`, porque no se instalan las dependencias del bot anterior. **No se probó el arranque en Render ni con un Redis real.**
- `pip-audit` sobre las seis dependencias fijadas: sin vulnerabilidades conocidas (las transitivas quedan sin fijar).

## Información comercial

Contrastada con `tugastroradar.com/planes` el 25-nov: Esencial gratis, Conecta $39.90, Presencia $89.90, banners $499 al mes por posición y ubicación, IVA incluido.

- El sitio anuncia "Primer mes sin costo" (no venía en el encargo).
- El sitio dice que **la contratación de planes aún no está disponible**; Vicky lo dice (`ONLINE_CONTRACTING_AVAILABLE=false`).
- El beneficio de la terminal Inbursa **no aparece** en esa página: no se menciona hasta poner `TPV_BENEFIT_VERIFIED=true`.
- No se verificó la configuración interna de producción de GastroRadar.

## Costos mensuales estimados (USD, no cotización)

Supuestos: 500 conversaciones al mes con 6 respuestas cada una (3,000 mensajes), 100 avisos al asesor.

| Concepto | Estimado | Confianza |
|---|---|---|
| Render web, plan Starter | 7 | Fuente de búsqueda; confirmar. Quizá ya se paga |
| Render Key Value con persistencia | desde ~10 | Fuente de terceros; quizá ya se paga |
| Meta: respuestas, (3,000 − 1,000 gratis) × 0.0085 | 17 | Fuentes de terceros sobre el cambio del 1-oct-2026 |
| Meta: 100 plantillas de utilidad × 0.0085 | 0.85 | Igual |
| OpenAI `gpt-4o-mini` (0.15 / 0.60 por millón), tope de 300 llamadas al día, 700 + 150 tokens | máx. ~1.76 | Precio de agregador; apagada hoy = 0 |
| Google Sheets | 0 | Dentro de cuotas habituales |

Total aproximado: **18 a 37**, según lo que ya se pague. Si el número no tiene los 1,000 mensajes gratis, sumar 8.50. Confirmar tarifas en [Meta](https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing), [Render](https://render.com/pricing) y OpenAI.

## Pendientes que bloquean el despliegue

1. Decidir el destino de `/ext/lead`, el Flow de IMSS, Boardroom, Radar, Rodys y las conversaciones financieras abiertas.
2. Verificar Render: commit, plan, Key Value persistente y registros del problema de 24 h.
3. Verificar número, `phone_number_id`, token permanente y qué apps de Meta comparten la cuenta con SECOM.
4. Aprobar la plantilla del asesor y confirmar un aviso `delivered`.
5. Ensayo real con número de prueba: WhatsApp, Redis, Sheets y arranque en Render.
6. Actualizar el aviso de privacidad y definir retención y borrado.
7. Confirmar "primer mes sin costo" y condiciones del beneficio de la terminal.
8. Si se quiere IA: confirmar que el modelo sigue disponible y probarlo con conversaciones reales.
9. Segunda auditoría independiente.

## Riesgos que quedan

- Las reglas de lenguaje no cubren toda forma de pedir la baja. Conviene revisar conversaciones al inicio.
- "No me llamen antes de las 5" se toma como baja completa (se prefirió pecar de cauteloso).
- Un envío incierto requiere revisión humana; una respuesta podría no llegar.
- Los problemas de entrega quedan en `/admin` y en registros: **no hay alerta automática** si falla el aviso al asesor.
- Si Redis pierde datos, se pierden bajas y prospectos no copiados a Sheets.
- Meta permite asistentes de un negocio, pero conviene revisar su política vigente sobre IA antes de encenderla.
