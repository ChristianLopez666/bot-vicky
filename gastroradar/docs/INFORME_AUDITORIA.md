# Informe de auditoría — Vicky GastroRadar v1 (25-nov-2026)

## Dictamen: NO APTO PARA DESPLIEGUE

**Actualización:** hallazgos de la auditoría de Work, 24 horas, costos y pruebas reales pendientes en [CIERRE_ISSUE_24.md](CIERRE_ISSUE_24.md). Ese documento manda sobre este.

El código pasa sus pruebas locales con servicios simulados. No se ha probado contra WhatsApp, un Redis real, Google Sheets, OpenAI ni Render.

El `app.py` v0.1 no se recibió. Esta versión se **escribió desde cero**: no es una auditoría de la v0.1. La auditoría es propia; falta la revisión independiente prevista.

## Etapa A — Diagnóstico (solo lectura)

| Punto | Resultado |
|---|---|
| Commit en GitHub | `main` = `dc6f64d` (18-sep). PR 16 abierto. Repositorio **público** |
| Versión en Render | `dc6f64d`, igual que `main`. Auto-Deploy activado, plan Starter |
| Salud actual | `/health` 200 una vez, `sheets: true` |
| Arranque | Render usa `gunicorn app:app`; `render.yaml` dice `filtered_app:app`. En producción no se aplica ese filtro de bitácora |
| Número conectado | **No verificado** |
| Aviso al asesor anterior | Texto libre con respaldo de plantilla (`ADVISOR_TEMPLATE_NAME`) |
| Redis | El único Key Value de la cuenta es gratuito, sin persistencia y con `allkeys_lru`: **no sirve para la versión nueva**. No se verificó qué servicio lo usa |
| Sheets | `/health` indica activo; contenido no verificado |
| Respaldo | Paquete git creado, verificado y restaurado localmente |
| Suite anterior | 567 pasan, 19 fallan; GitHub reporta lo mismo en `main` |
| Historial en busca de secretos | **No revisado** |

## Etapa E — Las 24 horas

**Causa real: no determinada.** Evidencia y lo que falta: [CIERRE_ISSUE_24.md](CIERRE_ISSUE_24.md#vgr-02--las-24-horas-pendiente-causa-no-determinada).

La versión nueva usa plantilla para el asesor, no responde mensajes con más de 24 h (salvo aplicar bajas y negativas) y registra cada fallo de entrega. No envía campañas.

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

## Costos

La estimación anterior usaba tarifas de terceros y queda **retirada**. Ver [CIERRE_ISSUE_24.md](CIERRE_ISSUE_24.md#vgr-03--costo-bloqueado-con-el-diseño-actual).

## Pendientes que bloquean el despliegue

1. Decidir el destino de `/ext/lead`, el Flow de IMSS, Boardroom, Radar, Rodys y las conversaciones financieras abiertas.
2. Elegir una alternativa de almacenamiento (ver cierre, VGR-03). Sin gasto adicional el diseño actual no es durable.
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
