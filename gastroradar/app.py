"""Vicky GastroRadar: webhook de WhatsApp Cloud API.

El webhook solo valida y encola en Redis; el trabajo ocurre en hilos del mismo
proceso. Arranque: gunicorn app:app --workers 1 --threads 8
"""
import hashlib
import hmac
import json
import logging
import os

import redis
from flask import Flask, jsonify, request

from vgr.config import Config, same_number
from vgr.service import Service
from vgr.store import P, Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("vgr.app")
MAX_BODY = 1024 * 1024


def valid_signature(secret: str, raw: bytes, header: str) -> bool:
    if not secret or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), header.encode("utf-8", "replace"))


def _equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8", "replace"), b.encode("utf-8", "replace"))


def build_service(cfg: Config) -> Service:
    client = redis.Redis.from_url(cfg.kv_url or "redis://127.0.0.1:6379/0", decode_responses=True,
                                  socket_timeout=3, socket_connect_timeout=3, health_check_interval=30)
    return Service(cfg, Store(client))


def create_app(cfg: Config = None, service: Service = None, start_workers: bool = True) -> Flask:
    cfg = cfg or Config.from_env()
    service = service or build_service(cfg)
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY
    app.extensions["vgr"] = service

    @app.get("/")
    def root():
        return jsonify(service="vicky-gastroradar", status="online")

    @app.get("/webhook")
    def verify():
        token = request.args.get("hub.verify_token", "")
        if request.args.get("hub.mode") == "subscribe" and cfg.verify_token and _equal(token, cfg.verify_token):
            return request.args.get("hub.challenge", ""), 200, {"Content-Type": "text/plain"}
        return "forbidden", 403

    @app.post("/webhook")
    def receive():
        raw = request.get_data()
        if not valid_signature(cfg.app_secret, raw, request.headers.get("X-Hub-Signature-256", "")):
            return jsonify(status="forbidden"), 403
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError
        except (ValueError, RecursionError):
            return jsonify(status="ignored"), 200  # firmado pero ilegible: reintentar no ayuda
        if not cfg.enabled:
            log.warning("webhook_ignorado motivo=bot_apagado")
            return jsonify(status="disabled"), 200
        try:
            queued = service.accept_webhook(payload)
        except Exception as exc:
            # Sin persistencia no se confirma: Meta reintentara.
            log.error("webhook_sin_persistencia tipo=%s", type(exc).__name__)
            return jsonify(status="unavailable"), 503
        return jsonify(status="ok", queued=queued), 200

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    @app.get("/ready")
    def ready():
        checks = {"configuracion": not cfg.missing_required(), "bot_encendido": cfg.enabled,
                  "asesor": not cfg.advisor_problem(), "plantilla_asesor": bool(cfg.advisor_template),
                  "sheets_configurado": service.sheets.configured}
        backlog = {}
        try:
            checks["redis"] = service.store.ping()
            beat = service.store.r.get(P + "heartbeat")
            checks["trabajador"] = bool(beat) and service.now - float(beat) < 90
            backlog = service.store.backlog()
            checks["cola_al_dia"] = backlog["atraso_segundos"] < 300
        except Exception:
            checks.update(redis=False, trabajador=False, cola_al_dia=False)
        critical = ("configuracion", "bot_encendido", "redis", "trabajador", "cola_al_dia", "asesor")
        ok = all(checks.get(k) for k in critical)
        return jsonify(ready=ok, checks=checks, cola=backlog,
                       ia="activa" if cfg.ai_enabled and cfg.openai_key else "apagada"), 200 if ok else 503

    def admin_ok():
        token = request.headers.get("X-Admin-Token", "")
        return len(cfg.admin_token) >= 24 and _equal(token, cfg.admin_token)

    def hidden():
        return jsonify(status="not_found"), 404

    @app.get("/admin/diagnostico")
    def diagnostics():
        if not admin_ok():
            return hidden()
        number = service.wa.check_number()
        persistence = "desconocida"
        try:
            info = service.store.r.info("persistence")
            persistence = {"aof": info.get("aof_enabled"), "rdb_ultimo": info.get("rdb_last_save_time"),
                           "politica_memoria": service.store.r.info("memory").get("maxmemory_policy")}
        except Exception:
            pass
        try:
            queue, problems = service.store.backlog(), service.delivery_problems(20)
        except Exception:
            queue, problems = "redis_no_disponible", []
        return jsonify(faltantes=cfg.missing_required(), asesor=cfg.advisor_problem() or "ok",
                       meta=dict(number, coincide_con_BOT_WHATSAPP_NUMBER=same_number(number["numero"],
                                                                                    cfg.bot_number)),
                       persistencia_redis=persistence, cola=queue, problemas_entrega=problems)

    @app.get("/admin/prospectos")
    def leads():
        if not admin_ok():
            return hidden()
        return jsonify(prospectos=service.list_leads())

    @app.get("/admin/fallidos")
    def failed():
        if not admin_ok():
            return hidden()
        items = [{"tipo": i["job"]["kind"], "motivo": i["reason"], "at": i["at"]} for i in service.store.dead()]
        return jsonify(trabajos=items, entregas=service.delivery_problems())

    @app.post("/admin/reintentar")
    def revive():
        if not admin_ok():
            return hidden()
        return jsonify(reencolados=service.store.revive())

    @app.post("/admin/prospectos/<lead_id>/avisar")
    def renotify(lead_id):
        if not admin_ok():
            return hidden()
        return jsonify(encolado=service.renotify(lead_id))

    if start_workers:
        service.start_workers()
    return app


if os.environ.get("VGR_NO_AUTOSTART") != "1":
    from dotenv import load_dotenv
    load_dotenv()
    app = create_app()
