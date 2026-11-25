"""Orquesta la cola: mensajes entrantes, prospectos, envios, estados y avisos.

Regla de concurrencia: un prospecto y un consentimiento SOLO se escriben desde
trabajos del carril "in:<telefono>". El resultado de los avisos se guarda en una
clave aparte para que nunca pueda sobrescribir una baja.
"""
import copy
import json
import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone

from . import flow, knowledge as kb
from .ai import AI
from .config import same_number
from .sheets import Sheets
from .store import P, LostLock, Tx, dumps, new_job
from .whatsapp import WhatsApp

log = logging.getLogger("vgr.service")

SESSION_TTL = 7 * 24 * 3600
WINDOW = 24 * 3600
FOLLOWUP_DELAY = 30 * 60
RENOTIFY_AFTER = 24 * 3600
OUT_TTL = 90 * 24 * 3600
RANK = {"pendiente": 0, "enviando": 1, "aceptado": 2, "sent": 3, "delivered": 4, "read": 5}
MX = timezone(timedelta(hours=-7))  # Sinaloa, sin horario de verano
MAX_ATTEMPTS = {"inbound": 8, "send": 6, "sheet": 20, "status": 6}


def stamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, MX).strftime("%Y-%m-%d %H:%M:%S")


def backoff(attempt: int) -> float:
    return min(3600, 5 * 2 ** (attempt - 1))


class Retry(Exception):
    def __init__(self, reason, delay=None):
        super().__init__(reason)
        self.reason, self.delay = reason, delay


class Service:
    def __init__(self, cfg, store, whatsapp=None, ai=None, sheets=None):
        self.cfg, self.store = cfg, store
        self.wa = whatsapp or WhatsApp(cfg)
        self.ai = ai or AI(cfg, store)
        self.sheets = sheets or Sheets(cfg)
        self.stop = threading.Event()
        self.threads = []

    @property
    def now(self):
        return self.store.clock()

    # ═══ recepcion (rapida: solo escribe en Redis) ══════════════════════
    def accept_webhook(self, payload: dict) -> int:
        """Encola lo que pertenece a este numero. Si Redis falla, propaga."""
        accepted = 0
        for entry in _list(payload.get("entry")):
            for change in _list(_dict(entry).get("changes")):
                value = _dict(_dict(change).get("value"))
                pid = str(_dict(value.get("metadata")).get("phone_number_id") or "")
                # La cuenta de WhatsApp es compartida: sin id propio exacto, se descarta.
                if not self.cfg.phone_number_id or pid != self.cfg.phone_number_id:
                    if value.get("messages") or value.get("statuses"):
                        log.info("webhook_descartado motivo=numero_ajeno_o_ausente")
                    continue
                for msg in _list(value.get("messages")):
                    msg = _dict(msg)
                    mid, sender = str(msg.get("id") or ""), str(msg.get("from") or "")
                    if not mid or len(mid) > 256 or not sender.isdigit() or not 8 <= len(sender) <= 15:
                        log.warning("mensaje_malformado")
                        continue
                    data = {"wa": sender, "mid": mid, "type": str(msg.get("type") or ""),
                            "text": _extract_text(msg), "ts": _timestamp(msg.get("timestamp"), self.now),
                            "referral": bool(msg.get("referral"))}
                    job = new_job("inbound", data, self.now, jid=mid)
                    accepted += self.store.enqueue_once("in:" + mid, "in:" + sender, job)
                for st in _list(value.get("statuses")):
                    st = _dict(st)
                    wamid, status = str(st.get("id") or ""), str(st.get("status") or "")
                    if not wamid or len(wamid) > 256 or status not in ("sent", "delivered", "read", "failed"):
                        continue
                    errors = [str(_dict(e).get("code")) for e in _list(st.get("errors"))]
                    job = new_job("status", {"wamid": wamid, "status": status, "errors": errors}, self.now)
                    accepted += self.store.enqueue_once(f"st:{wamid}:{status}", "st:" + wamid, job)
        return accepted

    # ═══ trabajador ═════════════════════════════════════════════════════
    def work_once(self) -> bool:
        claimed = self.store.claim()
        if not claimed:
            return False
        lane, token, job = claimed
        try:
            try:
                tx = getattr(self, "do_" + job["kind"])(job)
                self.store.done(lane, token, tx)
            except LostLock:
                raise
            except Exception as exc:
                self._failed(lane, token, job, exc)
        except LostLock:
            log.warning("carril_perdido tipo=%s", job["kind"])
        return True

    def _failed(self, lane, token, job, exc):
        reason = exc.reason if isinstance(exc, Retry) else type(exc).__name__
        if reason == "bot_apagado":
            self.store.retry(lane, token, job, exc.delay)  # esperar no gasta intentos
            return
        job["attempts"] += 1
        log.warning("trabajo_fallo tipo=%s intento=%s motivo=%s", job["kind"], job["attempts"], reason)
        if job["attempts"] >= MAX_ATTEMPTS.get(job["kind"], 8):
            tx = Tx()
            if job["kind"] == "send":
                record = self.store.get_json(f"out:{job['data']['out_id']}")
                if record:
                    self._mark_out(tx, record, "fallido", reason)
            self.store.bury(lane, token, job, reason, tx)
            log.error("trabajo_agotado tipo=%s motivo=%s", job["kind"], reason)
        else:
            delay = getattr(exc, "delay", None)
            self.store.retry(lane, token, job, backoff(job["attempts"]) if delay is None else delay)

    def drain(self, limit=500) -> int:
        n = 0
        while n < limit and self.work_once():
            n += 1
        return n

    def start_workers(self):
        def loop(index):
            while not self.stop.is_set():
                try:
                    if index == 0:
                        self.store.r.set(P + "heartbeat", str(self.now), ex=120)
                    if not self.work_once():
                        self.stop.wait(0.5)
                except Exception as exc:  # Redis caido: esperar y volver
                    log.error("trabajador_error tipo=%s", type(exc).__name__)
                    self.stop.wait(5)
        for i in range(max(1, self.cfg.worker_threads)):
            thread = threading.Thread(target=loop, args=(i,), daemon=True, name=f"vgr-{i}")
            thread.start()
            self.threads.append(thread)

    # ═══ mensaje entrante ═══════════════════════════════════════════════
    def do_inbound(self, job):
        d, tx = job["data"], Tx()
        wa = d["wa"]
        if same_number(wa, self.cfg.bot_number):
            return tx
        if same_number(wa, self.cfg.advisor_number):
            # Nunca se le contesta al asesor: evita bucles. Solo se anota su ventana de 24h.
            tx.set(P + "advisor_window", str(d["ts"]), ex=WINDOW)
            return tx
        if d["type"] in ("reaction", "system", "unsupported_event"):
            return tx

        session = self.store.get_json(f"session:{wa}", {}) or {}
        consent = self.store.get_json(f"consent:{wa}", {}) or {}
        consented = consent.get("status") == "otorgado"
        text = d["text"]
        optout = bool(text) and flow.is_optout(text)

        if self.now - d["ts"] > WINDOW - 60 and not optout:
            # Fuera de la ventana de WhatsApp: no se avanza un dialogo que no se puede contestar.
            self._problem(tx, "r:" + job["id"], "reply", "", "expirado", "mensaje_antiguo")
            return tx

        rate_key = P + f"rate:{wa}:{int(self.now // 600)}"
        count = int(self.store.r.get(rate_key) or 0) + 1
        tx.set(rate_key, count, ex=1200)
        if count > self.cfg.user_msgs_per_10min and not optout:
            if count == self.cfg.user_msgs_per_10min + 1:
                self._reply(tx, wa, "Recibí muchos mensajes seguidos. Espera unos minutos y vuelve a escribir.",
                            d, job["id"])
            return tx

        if not text:
            res = flow.Result()
            res.say("Por ahora solo puedo leer mensajes de texto. Escribe tu consulta o MENU.")
        else:
            draft = copy.deepcopy(session)
            res = flow.handle(draft, text, consented, self.cfg)
            if res.needs_ai:
                draft = copy.deepcopy(session)
                answer = self.ai.ask(wa, text, session.get("history", []))
                res = flow.handle(draft, text, consented, self.cfg, ai=answer or False)
            session = draft

        if not session.get("greeted"):
            session["greeted"] = True
            if res.replies and not res.replies[0].startswith("Hola, soy Vicky"):
                res.replies[0] = "Soy Vicky, asistente virtual de GastroRadar.\n" + res.replies[0]
        reply = "\n\n".join(res.replies)

        self._apply_events(tx, wa, session, consent, res.events, d)
        history = session.get("history", [])
        if text:
            history = history + [{"role": "user", "text": text[:300]}, {"role": "assistant", "text": reply[:300]}]
        session["history"] = history[-6:]
        tx.set(P + f"session:{wa}", dumps(session), ex=SESSION_TTL)
        if reply:
            self._reply(tx, wa, reply, d, job["id"])
        return tx

    def _reply(self, tx, wa, text, d, source_id):
        self._queue_send(tx, "r:" + source_id, "out:" + wa, "reply",
                         {"to": wa, "type": "text", "text": {"body": text[:4000]}},
                         expires=d["ts"] + WINDOW - 60)

    def _queue_send(self, tx, out_id, lane, purpose, payload, expires=None, lead_id=""):
        record = {"id": out_id, "purpose": purpose, "lead_id": lead_id, "status": "pendiente",
                  "wamid": "", "detail": "", "created": self.now, "payload": payload, "expires": expires}
        tx.set(P + f"out:{out_id}", dumps(record), ex=OUT_TTL)
        tx.push(lane, new_job("send", {"out_id": out_id}, self.now), self.now)

    # ═══ prospectos y consentimiento ════════════════════════════════════
    def _apply_events(self, tx, wa, session, consent, events, d):
        if not events:
            return
        now = self.now
        leads = self.store.get_json(f"leads_of:{wa}", {}) or {}
        changed = {}

        def load(lead_id):
            return changed.get(lead_id) or self.store.get_json(f"lead:{lead_id}")

        def consent_log(status):
            consent.update({"status": status, "updated": now, "version": kb.CONSENT_VERSION,
                            "message_id": d["mid"]})
            # Sin vencimiento: la baja debe sobrevivir a cualquier sesion.
            tx.set(P + f"consent:{wa}", dumps(consent))
            tx.rpush(P + f"consent_log:{wa}", dumps(consent))

        for event, service in events:
            if event == "consent_denied":
                consent_log("rechazado")
            elif event in ("consent_revoked", "data_deletion"):
                consent_log("revocado")
                for lead_id in leads.values():
                    lead = load(lead_id)
                    if not lead:
                        continue
                    first_time = lead["consent"] != "revocado"
                    if first_time:
                        lead.update(consent="revocado", status="No contactar")
                        self._history(lead, "consentimiento revocado")
                    if event == "data_deletion":
                        self._history(lead, "pidió borrado de datos")
                    changed[lead_id] = lead
                    if (first_time and lead["notified"]) or event == "data_deletion":
                        title = "PIDE BORRAR DATOS" if event == "data_deletion" else "BAJA: NO CONTACTAR"
                        self._notify_advisor(tx, lead, title, f"rev-{lead_id}-{d['mid']}")
            else:
                if event == "consent_granted":
                    consent_log("otorgado")
                lead_id = leads.get(service)
                lead = load(lead_id) if lead_id else None
                if not lead:
                    lead_id = f"GR-{stamp(now)[:10].replace('-', '')}-{uuid.uuid4().hex[:8]}"
                    leads[service] = lead_id
                    lead = {"id": lead_id, "created": now, "wa": wa, "name": "", "business": "",
                            "municipality": "", "service": service,
                            "origin": "WhatsApp (anuncio)" if d.get("referral") else "WhatsApp",
                            "consent": "", "status": "Incompleto", "history": [], "notified": False,
                            "complete_notified": False, "notified_at": 0}
                    self._history(lead, "creado")
                    tx.push("fu:" + lead_id, new_job("followup_due", {"lead_id": lead_id, "wa": wa}, now),
                            now + FOLLOWUP_DELAY)
                if lead["consent"] != "otorgado":
                    if lead["consent"] == "revocado":
                        lead.update(status="Incompleto", notified=False, complete_notified=False)
                    lead["consent"] = "otorgado"
                    self._history(lead, "consentimiento otorgado " + kb.CONSENT_VERSION)
                for name in ("name", "business", "municipality"):
                    value = session.get("profile", {}).get(name)
                    if value and lead[name] != value:
                        lead[name] = value
                        self._history(lead, f"{name} actualizado")
                if event == "lead_ready":
                    stale = lead["notified"] and now - lead["notified_at"] > RENOTIFY_AFTER
                    first_complete = not lead["complete_notified"]
                    if first_complete or stale:
                        title = ("Prospecto insiste" if not first_complete else
                                 "Datos completos" if lead["notified"] else "Nuevo prospecto")
                        lead.update(status="Nuevo", notified=True, complete_notified=True, notified_at=now)
                        self._history(lead, "datos completos" if first_complete else "volvió a pedir contacto")
                        self._notify_advisor(tx, lead, title, f"new-{lead_id}-{d['mid']}")
                changed[lead_id] = lead
        tx.set(P + f"leads_of:{wa}", dumps(leads))
        for lead in changed.values():
            self._save_lead(tx, lead)

    def _history(self, lead, what):
        lead["history"] = (lead["history"] + [f"{stamp(self.now)} {what}"])[-40:]
        lead["updated"] = self.now

    def _save_lead(self, tx, lead):
        tx.set(P + f"lead:{lead['id']}", dumps(lead))
        tx.zadd(P + "leads", {lead["id"]: lead["created"]})
        self._queue_sheet(tx, lead["id"])

    def _queue_sheet(self, tx, lead_id):
        tx.push("sheet:" + lead_id, new_job("sheet", {"lead_id": lead_id}, self.now), self.now)

    def _set_delivery(self, tx, lead_id, status):
        tx.set(P + f"delivery:{lead_id}", status)
        tx.rpush(P + f"delivery_log:{lead_id}", f"{stamp(self.now)} {status}")

    def _notify_advisor(self, tx, lead, title, suffix):
        problem = self.cfg.advisor_problem()
        if not problem and not self.cfg.advisor_template and not self.store.r.get(P + "advisor_window"):
            problem = "sin_plantilla_y_ventana_cerrada"
        if problem:
            self._set_delivery(tx, lead["id"], "bloqueado:" + problem)
            self._problem(tx, "adv:" + suffix, "advisor", lead["id"], "fallido", problem)
            return
        summary = (f"{title} | {lead['id']} | {kb.SERVICES.get(lead['service'], lead['service'])} | "
                   f"Nombre: {lead['name'] or 's/d'} | Negocio: {lead['business'] or 's/d'} | "
                   f"Municipio: {lead['municipality'] or 's/d'} | WhatsApp: {lead['wa']}")
        if self.cfg.advisor_template:
            payload = {"to": self.cfg.advisor_number, "type": "template", "template": {
                "name": self.cfg.advisor_template, "language": {"code": self.cfg.advisor_template_lang},
                "components": [{"type": "body", "parameters": [{"type": "text", "text": summary[:900]}]}]}}
        else:
            payload = {"to": self.cfg.advisor_number, "type": "text", "text": {"body": summary}}
        self._set_delivery(tx, lead["id"], "pendiente")
        urgent_stop = title.startswith(("BAJA", "PIDE BORRAR"))
        self._queue_send(tx, "adv:" + suffix, "out:advisor",
                         "advisor_stop" if urgent_stop else "advisor", payload, lead_id=lead["id"])

    def do_followup_due(self, job):
        """Pasa el seguimiento al carril del usuario para escribir sin carreras."""
        tx = Tx()
        tx.push("in:" + job["data"]["wa"], new_job("followup", job["data"], self.now), self.now)
        return tx

    def do_followup(self, job):
        """Prospecto con autorizacion que no termino de dar datos: se avisa igual."""
        tx = Tx()
        lead = self.store.get_json(f"lead:{job['data']['lead_id']}")
        if lead and lead["consent"] == "otorgado" and not lead["notified"]:
            lead.update(notified=True, notified_at=self.now)
            self._history(lead, "aviso con datos incompletos")
            self._notify_advisor(tx, lead, "Prospecto incompleto", f"inc-{lead['id']}")
            self._save_lead(tx, lead)
        return tx

    def do_renotify(self, job):
        tx = Tx()
        lead = self.store.get_json(f"lead:{job['data']['lead_id']}")
        if lead and lead["consent"] == "otorgado":
            lead.update(notified=True, notified_at=self.now)
            self._history(lead, "reenvío manual del aviso")
            self._notify_advisor(tx, lead, "Prospecto (reenvío)", f"man-{lead['id']}-{job['id'][:8]}")
            self._save_lead(tx, lead)
        return tx

    def renotify(self, lead_id) -> bool:
        """Reenvio decidido por una persona (por ejemplo, tras un estado incierto)."""
        lead = self.store.get_json(f"lead:{lead_id}")
        if not lead or lead["consent"] != "otorgado":
            return False
        tx = Tx()
        tx.push("in:" + lead["wa"], new_job("renotify", {"lead_id": lead_id}, self.now), self.now)
        self.store.commit(tx)
        return True

    # ═══ envios ═════════════════════════════════════════════════════════
    def _problem(self, tx, out_id, purpose, lead_id, status, detail):
        tx.lpush(P + "delivery_problems", dumps({"out_id": out_id, "purpose": purpose, "lead_id": lead_id,
                                                 "status": status, "detail": detail, "at": self.now}))
        tx.ltrim(P + "delivery_problems", 0, 499)
        log.error("entrega_problema estado=%s proposito=%s detalle=%s lead=%s",
                  status, purpose, detail, lead_id or "-")

    def _mark_out(self, tx, record, status, detail="", wamid=""):
        record = dict(record, status=status, detail=detail)
        if wamid:
            record["wamid"] = wamid
        tx.set(P + f"out:{record['id']}", dumps(record), ex=OUT_TTL)
        if record["lead_id"]:
            self._set_delivery(tx, record["lead_id"], status)
            self._queue_sheet(tx, record["lead_id"])
        if status in ("fallido", "incierto", "expirado"):
            self._problem(tx, record["id"], record["purpose"], record["lead_id"], status, detail)

    def _save_out(self, record, status):
        self.store.r.set(P + f"out:{record['id']}", dumps(dict(record, status=status)), ex=OUT_TTL)

    def do_send(self, job):
        tx = Tx()
        record = self.store.get_json(f"out:{job['data']['out_id']}")
        if not record or record["status"] not in ("pendiente", "enviando"):
            return tx
        if record["status"] == "enviando":
            # El proceso cayo entre llamar a Meta y guardar la respuesta.
            self._mark_out(tx, record, "incierto", "reinicio_durante_envio")
            return tx
        if not self.cfg.enabled:
            raise Retry("bot_apagado", 300)
        if record["expires"] and self.now > record["expires"]:
            self._mark_out(tx, record, "expirado", "ventana_24h_cerrada")
            return tx
        if record["purpose"] == "advisor" and record["lead_id"]:
            lead = self.store.get_json(f"lead:{record['lead_id']}")
            if lead and lead["consent"] != "otorgado":
                self._mark_out(tx, record, "cancelado", "consentimiento_revocado")
                return tx
        self._save_out(record, "enviando")
        result = self.wa.send(record["payload"])
        if result.kind == "retry":
            self._save_out(record, "pendiente")
            raise Retry(result.detail)
        if result.kind == "accepted":
            self._mark_out(tx, record, "aceptado", wamid=result.wamid)
            tx.set(P + f"wamid:{result.wamid}", record["id"], ex=OUT_TTL)
        else:
            self._mark_out(tx, record, "fallido" if result.kind == "rejected" else "incierto", result.detail)
        return tx

    def do_status(self, job):
        tx, d = Tx(), job["data"]
        out_id = self.store.r.get(P + f"wamid:{d['wamid']}")
        if not out_id:
            if job["attempts"] >= 3:
                return tx  # estado de un mensaje que no enviamos
            raise Retry("wamid_desconocido", 5)
        record = self.store.get_json(f"out:{out_id}")
        if not record:
            return tx
        current, new = record["status"], d["status"]
        if new == "failed":
            if current not in ("delivered", "read"):
                self._mark_out(tx, record, "fallido", "meta_" + ",".join(d["errors"]))
        elif current != "fallido" and RANK.get(new, 0) > RANK.get(current, 0):
            self._mark_out(tx, record, new)
        return tx

    # ═══ hoja de calculo ════════════════════════════════════════════════
    def lead_row(self, lead):
        delivery = self.store.r.get(P + f"delivery:{lead['id']}") or "sin_aviso"
        return [lead["id"], stamp(lead["created"]), lead["name"], lead["wa"], lead["business"],
                lead["municipality"], kb.SERVICES.get(lead["service"], lead["service"]), lead["origin"],
                lead["consent"], lead["status"], stamp(lead["updated"]),
                " | ".join(lead["history"]) + " | aviso: " + delivery]

    def do_sheet(self, job):
        lead = self.store.get_json(f"lead:{job['data']['lead_id']}")
        if lead:
            self.sheets.upsert(self.lead_row(lead))  # siempre la version mas reciente
        return Tx()

    # ═══ consultas de recuperacion ══════════════════════════════════════
    def list_leads(self, limit=200):
        out = []
        for lead_id in self.store.r.zrevrange(P + "leads", 0, limit - 1):
            lead = self.store.get_json(f"lead:{lead_id}")
            if lead:
                lead["advisor_delivery"] = self.store.r.get(P + f"delivery:{lead_id}") or "sin_aviso"
                out.append(lead)
        return out

    def delivery_problems(self, limit=100):
        return [json.loads(x) for x in self.store.r.lrange(P + "delivery_problems", 0, limit - 1)]


def _dict(v):
    return v if isinstance(v, dict) else {}


def _list(v):
    return v if isinstance(v, list) else []


def _timestamp(v, now):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return now
    return min(n, now) if n > 0 else now


def _extract_text(msg: dict) -> str:
    kind = msg.get("type")
    if kind == "text":
        body = _dict(msg.get("text")).get("body")
    elif kind == "button":
        body = _dict(msg.get("button")).get("text")
    elif kind == "interactive":
        inner = _dict(msg.get("interactive"))
        body = _dict(inner.get("button_reply") or inner.get("list_reply")).get("title")
    else:
        body = ""
    return body.strip()[:1500] if isinstance(body, str) else ""
