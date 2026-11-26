"""Persistencia y cola sobre Redis/Valkey.

No hay respaldo en memoria: si Redis falla, el webhook responde 503 para que
Meta reintente, en lugar de aceptar un mensaje que se perderia al reiniciar.

Cola por carriles: cada carril (p. ej. "in:<telefono>") es una lista FIFO.
Un trabajo solo sale de la lista cuando sus efectos se confirman en la MISMA
transaccion, asi que una caida a medias lo deja pendiente.
"""
import json
import time
import uuid

from redis.exceptions import WatchError

P = "vgr:"
DUE = P + "due"              # zset carril -> momento en que puede atenderse
DEAD = P + "dead"            # lista de trabajos agotados, recuperables
DEDUPE_TTL = 8 * 24 * 3600   # mas que la ventana de reintentos de Meta (7 dias)
LOCK_MS = 120_000            # mayor que el peor caso de un trabajo


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def new_job(kind: str, data: dict, now: float, jid: str = "") -> dict:
    return {"id": jid or uuid.uuid4().hex, "kind": kind, "data": data, "attempts": 0, "created": now}


class LostLock(Exception):
    pass


class Tx:
    """Efectos que se aplican juntos al confirmar un trabajo."""

    def __init__(self):
        self.ops = []

    def _add(self, name, *args, **kwargs):
        self.ops.append((name, args, kwargs))

    def set(self, key, value, **kwargs):
        self._add("set", key, value, **kwargs)

    def rpush(self, key, value):
        self._add("rpush", key, value)

    def lpush(self, key, value):
        self._add("lpush", key, value)

    def ltrim(self, key, start, end):
        self._add("ltrim", key, start, end)

    def zadd(self, key, mapping, **kwargs):
        self._add("zadd", key, mapping, **kwargs)

    def push(self, lane: str, job: dict, due: float):
        self.rpush(P + "lane:" + lane, dumps(job))
        # nx: no adelantar un carril que esta esperando reintento.
        self.zadd(DUE, {lane: due}, nx=True)


class Store:
    def __init__(self, redis_client, clock=time.time):
        self.r = redis_client
        self.clock = clock

    def get_json(self, key, default=None):
        raw = self.r.get(P + key)
        return json.loads(raw) if raw else default

    def ping(self) -> bool:
        return bool(self.r.ping())

    def enqueue_once(self, dedupe_id: str, lane: str, job: dict) -> bool:
        """Marca de duplicado y encolado en una sola transaccion."""
        key = P + "seen:" + dedupe_id
        with self.r.pipeline() as pipe:
            while True:
                try:
                    pipe.watch(key)
                    if pipe.exists(key):
                        return False
                    pipe.multi()
                    pipe.set(key, "1", ex=DEDUPE_TTL)
                    pipe.rpush(P + "lane:" + lane, dumps(job))
                    pipe.zadd(DUE, {lane: self.clock()}, nx=True)
                    pipe.execute()
                    return True
                except WatchError:
                    continue

    def commit(self, tx: Tx):
        pipe = self.r.pipeline(transaction=True)
        self._apply(pipe, tx)
        pipe.execute()

    @staticmethod
    def _apply(pipe, tx: Tx):
        for name, args, kwargs in tx.ops:
            getattr(pipe, name)(*args, **kwargs)

    def acquire(self, lane, token) -> bool:
        return bool(self.r.set(P + "lock:" + lane, token, nx=True, px=LOCK_MS))

    def pending(self, lane) -> int:
        return self.r.llen(P + "lane:" + lane)

    def claim(self):
        """Toma un carril vencido y libre. Devuelve (carril, token, trabajo) o None."""
        now = self.clock()
        for lane in self.r.zrangebyscore(DUE, "-inf", now, start=0, num=50):
            token = uuid.uuid4().hex
            if not self.acquire(lane, token):
                continue
            raw = self.r.lindex(P + "lane:" + lane, 0)
            score = self.r.zscore(DUE, lane)
            if raw is None or score is None or score > now:
                if raw is None:
                    self.r.zrem(DUE, lane)
                self.release(lane, token)
                continue
            return lane, token, json.loads(raw)
        return None

    def release(self, lane, token):
        key = P + "lock:" + lane
        with self.r.pipeline() as pipe:
            try:
                pipe.watch(key)
                if pipe.get(key) == token:
                    pipe.multi()
                    pipe.delete(key)
                    pipe.execute()
            except WatchError:
                pass

    def _finish(self, lane, token, build):
        """Ejecuta build(pipe, largo) solo si aun somos duenos del carril."""
        lock, lkey = P + "lock:" + lane, P + "lane:" + lane
        with self.r.pipeline() as pipe:
            while True:
                try:
                    pipe.watch(lock, lkey)
                    if pipe.get(lock) != token:
                        raise LostLock(lane.split(":")[0])
                    length = pipe.llen(lkey)
                    pipe.multi()
                    build(pipe, length)
                    pipe.delete(lock)
                    pipe.execute()
                    return
                except WatchError:
                    continue

    def done(self, lane, token, tx: Tx = None):
        def build(pipe, length):
            pipe.lpop(P + "lane:" + lane)
            if length <= 1:
                pipe.zrem(DUE, lane)
            else:
                pipe.zadd(DUE, {lane: self.clock()})
            if tx:
                self._apply(pipe, tx)   # despues: puede reencolar en este carril
        self._finish(lane, token, build)

    def retry(self, lane, token, job, delay: float):
        def build(pipe, _length):
            pipe.lset(P + "lane:" + lane, 0, dumps(job))
            pipe.zadd(DUE, {lane: self.clock() + delay})
        self._finish(lane, token, build)

    def bury(self, lane, token, job, reason: str, tx: Tx = None):
        tx = tx or Tx()
        tx.rpush(DEAD, dumps({"lane": lane, "job": job, "reason": reason, "at": self.clock()}))
        self.done(lane, token, tx)

    def backlog(self) -> dict:
        oldest = self.r.zrange(DUE, 0, 0, withscores=True)
        lag = max(0.0, self.clock() - oldest[0][1]) if oldest else 0.0
        return {"carriles_pendientes": self.r.zcard(DUE), "atraso_segundos": round(lag, 1),
                "trabajos_fallidos": self.r.llen(DEAD)}

    def dead(self, limit=100):
        return [json.loads(x) for x in self.r.lrange(DEAD, 0, limit - 1)]

    def revive(self, kinds=("sheet", "inbound", "status", "followup_due", "followup", "renotify")) -> int:
        """Reencola fallidos. Los envios de WhatsApp quedan fuera a proposito:
        reenviarlos a ciegas puede duplicar mensajes."""
        moved = 0
        for raw in self.r.lrange(DEAD, 0, -1):
            item = json.loads(raw)
            if item["job"]["kind"] not in kinds:
                continue
            job = dict(item["job"], attempts=0)
            pipe = self.r.pipeline(transaction=True)
            pipe.lrem(DEAD, 1, raw)
            pipe.rpush(P + "lane:" + item["lane"], dumps(job))
            # Reintento pedido por una persona: se atiende ya, aunque el carril estuviera en pausa.
            pipe.zadd(DUE, {item["lane"]: self.clock()})
            pipe.execute()
            moved += 1
        return moved
