# Contrato de Aceptación — RAG / Knowledge Base · bot-vicky-redes (v1)

> **Documento de gobernanza. No es código ni autorización de construcción.**
> Estado: aprobado conceptualmente por Don Chiwy con `PASS_WITH_REQUIRED_CHANGES`.
> Fecha: 2026-06-17 · Repo: `ChristianLopez666/bot-vicky` · Servicio: `bot-vicky-redes`.

```md
DICTAMEN_BASE: PASS_WITH_REQUIRED_CHANGES (aceptado)
RECOMENDACIÓN: Camino B (RAG embeddings) con FAIL-CLOSED obligatorio
CONSTRUCTOR: GPT · AUDITOR: Claude · APROBADOR: DON_CHIWY
CODE_CHANGE_AUTHORIZED: false
DEPLOY_AUTHORIZED: false
MERGE_TO_MAIN_AUTHORIZED: false
```

## Contexto del problema
Hoy `bot-vicky-redes` (entrypoint `app.py`, `gunicorn app:app`) responde toda pregunta abierta
—comercial o de procedimiento— con `ask_gpt()` → OpenAI `gpt-4o-mini` y un system prompt
hardcodeado (`_SYS`). **Ningún manual autorizado se consulta.** El código legacy de lectura de
manual (`read_manual_imss.py`, `flujo_imss.py`, `app_skeleton.py`, `webhook_handler.py`) está
huérfano (no lo importa `app.py`) y roto (PyMuPDF ausente en requirements, API OpenAI v0.x,
`eval()` sobre credenciales). Objetivo: que Vicky responda con **fragmentos recuperados de los
manuales autorizados**, y que **falle cerrado** cuando no haya respaldo.

## 0. Prerrequisitos BLOQUEANTES (la construcción NO inicia sin esto)
1. **File IDs exactos** de los 6 PDFs en Drive (no nombres).
2. **`client_email`** del service account (campo dentro de `GOOGLE_CREDENTIALS_JSON`) al que se
   compartirán los PDFs. *No se extrae del secreto; lo confirma Don Chiwy.*
3. **Permiso verificado:** ese `client_email` con rol **Lector** sobre los 6 PDFs (o su carpeta).
   Verificar, no asumir.
4. **Aclaración de mapeo "daños":** hoy `detect_svc` NO distingue *seguro de daños / comercio
   integral* del *crédito empresarial* (`emp`/`fp` son productos de **crédito**, sin manual; el
   manual de daños es de **seguro** de daños). Decidir para v1: ¿se agrega un disparador mínimo
   `danos` o se deja fuera? Sin esto, el manual de daños no tiene ruta de entrada.

## 1. Alcance
- **Dentro:** RAG sobre los manuales autorizados, detrás de `KB_RAG_ENABLED`, en `bot-vicky-redes`
  (`app.py` + módulo nuevo).
- **Fuera (PROHIBIDO tocar en el PR de construcción):** Hydra / Boardroom, Rodis, SECOM, el hotfix
  de notificación, `_verify_sig`, `_log`, webhook, funnels, Render / variables (las define Don
  Chiwy), deploy.

## 2. Regla de oro — FAIL-CLOSED (centro del contrato)
Para una **duda comercial / procedimiento** con servicio detectado:

| Condición | Acción |
|---|---|
| `KB_RAG_ENABLED=false` | `ask_gpt` actual (permitido) |
| Flag ON · svc **con** manual · índice OK · `score ≥ KB_MIN_SCORE` | **Respuesta grounded** (solo fragmentos) |
| Flag ON · svc **con** manual · índice no disponible **o** `score < umbral` **o** error de recuperación | **FALLBACK CHRISTIAN** — *"Permíteme confirmarlo con Christian López para darte información exacta."* **NUNCA `ask_gpt`** |
| Flag ON · svc **sin** manual autorizado | `ask_gpt` actual o menú (permitido) |
| Saludo / menú (no es duda comercial/procedimiento) | flujo actual / menú (permitido) |

`ask_gpt` genérico **solo** permitido si: flag OFF, **o** svc sin manual, **o** no es duda
comercial/procedimiento. En cualquier otro caso con manual → grounded o Christian. Sin tercera opción.

## 3. Defaults y candados
- `KB_RAG_ENABLED` = **false** por default (entra apagado).
- **Prohibido** reutilizar/reactivar legacy: `read_manual_imss.py`, `flujo_imss.py`,
  `app_skeleton.py`, `webhook_handler.py`. (Recomendado marcarlos `# DEPRECATED — no usar`; su
  borrado va en saneo aparte.)
- Separación estricta de tres planos: **build offline** · **runtime prod** ·
  **endpoint `/ext/kb/reload` OPCIONAL** (no obligatorio en v1).

## 4. Especificación (resumen ejecutable)
- **Build offline** (`scripts/build_kb_index.py`): Drive `drive.readonly` por file ID → PyMuPDF
  texto/página → chunking ~600 tok / overlap 100 con metadata `{manual_key, manual_name, file_id,
  page, section}` (IMSS: `comercial|procedimiento`) → embeddings `text-embedding-3-small` →
  artefacto `kb_index.json` + `kb_vectors.npy` con `version`/`built_at` → sube a Drive.
- **Runtime** (`kb_rag.py`): boot carga artefacto (download Drive readonly → /tmp) a matriz numpy;
  `kb_answer(text, svc, mid)` = map → embed → cosine top-k → umbral → grounded/Christian; prompt
  estricto temp ≈ 0.2 *"responde SOLO con base en estos fragmentos; no inventes; si no alcanza,
  deriva a Christian"*; respuesta en lenguaje comercial natural.
- **app.py (mínimo):** init al boot; en los 3 sitios `ask_gpt(text, svc)` (líneas 1461/1469/1473)
  enrutar por `kb_answer` **solo si** flag ON; conservar `ask_gpt` / `_SYS`. Nada más.
- **Mapeo v1:** auto → manual autos · imss → manual IMSS (secciones) · vida → manual vida **+** GMM ·
  vrim → manual VRIM · daños → manual daños *(pendiente prerreq #4)* · emp/fp (crédito) → sin manual.
- **Env nuevas:** `KB_RAG_ENABLED`, `KB_INDEX_FILE_ID`, `KB_MANUAL_*_FILE_ID`, `KB_EMBED_MODEL`,
  `KB_TOP_K`, `KB_MIN_SCORE`. Default flag false.
- **requirements:** prod `+numpy`; build `+PyMuPDF`.

### Mapeo servicio → manual autorizado
| svc (`detect_svc`) | Manual |
|---|---|
| auto | Manual para seguro de autos.PDF |
| imss | Manual credito IMSS Ley 73.pdf (secciones comercial / procedimiento) |
| vida | Manual para Seguro de vida.pdf **+** Manual de gastos médicos mayores |
| vrim | Manual gastos medicos menores membresías VRIM… |
| daños | Manual para seguro de daños.pdf *(pendiente disparador, prerreq #4)* |
| emp / fp (crédito) | sin manual en v1 → comportamiento actual |

## 5. Auditoría por fragmento (obligatoria)
Cada respuesta grounded emite traza determinista (Render logs):
`kb_audit mid=… svc=… manual=… section=… chunks=[id:score, …] fallback=false`.
Cada fallback Christian: `kb_audit … fallback=true reason=<no_index|low_score|retrieval_error>`.
Opcional persistir en Redis / pestaña `KB_AUDIT` (sin tocar CONVERSACIONES). La auditoría usa la
traza de recuperación determinista (chunk + página + score), no la auto-cita del modelo.

## 6. Pruebas doradas obligatorias (golden set)
1. **auto:** amplia vs limitada → grounded del manual de autos.
2. **IMSS comercial:** requisitos Ley 73 → sección comercial.
3. **IMSS procedimiento:** SIPRE / SUAP / carta de libranza / video-selfie → sección procedimiento.
4. **vida:** beneficiarios / cobertura.
5. **GMM:** deducible / coaseguro / reclamación.
6. **VRIM:** beneficios / restricciones.
7. **daños:** cobertura / exclusiones *(según prerreq #4)*.
8. **fail-closed:** pregunta con manual pero sin fragmento relevante → Christian, **no** GPT.
9. **flag OFF:** comportamiento idéntico al actual.
10. **sin índice:** artefacto ausente con flag ON y svc con manual → Christian, webhook 200, sin crash.

## 7. Checklist de auditoría Claude (antes de recomendar liberación)
- [ ] Fail-closed verificado en los 4 caminos de la tabla §2 (con evidencia / test).
- [ ] `KB_RAG_ENABLED=false` reproduce el comportamiento actual exacto.
- [ ] Cero imports / uso de los 4 módulos legacy.
- [ ] `app.py` sin cambios fuera de los 3 call-sites + init + env (diff mínimo).
- [ ] Traza de auditoría presente y recuperable por `message_id` en los 10 golden.
- [ ] Sin llamadas síncronas que rompan el timeout de Render; webhook responde 200 en error.
- [ ] No toca SECOM / Rodis / Hydra / Boardroom / notify-hotfix / `_verify_sig` / `_log` / funnels.
- [ ] Scope Drive del build = `drive.readonly`; prerrequisitos 1-4 confirmados antes de mergear.

## 8. Entregables del PR de construcción (uno solo, RAG)
Diff exacto, archivos nuevos/modificados, evidencia de los 10 golden, traza de auditoría de
ejemplo, instrucciones de build del índice, lista de env (sin valores). Rama nueva,
**sin deploy, sin commit a main sin autorización de Don Chiwy.**

## 9. Rollback
- Primario (sin deploy): `KB_RAG_ENABLED=false` → restart → comportamiento actual.
- Secundario: `git revert` del PR de construcción.
- Inerte con flag off; nada destructivo.

---

```md
READY_FOR_AUDIT: true
DEPLOY_AUTHORIZED: false
CODE_CHANGE_AUTHORIZED: false
MERGE_TO_MAIN_AUTHORIZED: false
BLOQUEANTES_PENDIENTES: file IDs · client_email · permiso Drive · mapeo daños
APPROVER_REQUIRED: DON_CHIWY
CONFIDENCE: 0.88
```

> **Gobernanza:** GPT construye contra este contrato → Claude audita el resultado →
> Don Chiwy aprueba → deploy. Este documento no autoriza construcción ni deploy.
