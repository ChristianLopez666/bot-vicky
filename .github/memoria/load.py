"""Envía el recorrido de la prueba real y mide memoria de todo gunicorn y tiempos."""
import hashlib
import hmac
import itertools
import json
import os
import sys
import threading
import time
import urllib.request

MASTER = int(sys.argv[1])
LABEL = sys.argv[2]
ids = itertools.count(1)


def pids():
    out, frontier = [MASTER], [MASTER]
    while frontier:
        parent = frontier.pop()
        try:
            with open(f"/proc/{parent}/task/{parent}/children") as handle:
                kids = [int(k) for k in handle.read().split()]
        except OSError:
            kids = []
        out += kids
        frontier += kids
    return out


def memory_mb():
    total = 0
    for pid in pids():
        try:
            with open(f"/proc/{pid}/smaps_rollup") as handle:
                total += next(int(line.split()[1]) for line in handle if line.startswith("Pss:"))
        except (OSError, StopIteration):
            pass
    return total / 1024


def one(pid):
    try:
        with open(f"/proc/{pid}/smaps_rollup") as handle:
            return next(int(line.split()[1]) for line in handle if line.startswith("Pss:")) / 1024
    except (OSError, StopIteration):
        return 0.0


def breakdown(moment):
    rows = []
    for pid in pids():
        with open(f"/proc/{pid}/status") as handle:
            threads = next(int(x.split()[1]) for x in handle if x.startswith("Threads:"))
        rows.append(f"{'maestro' if pid == MASTER else 'worker'}={one(pid):.0f}MiB/{threads}hilos")
    print(f"  {LABEL}: {moment}: procesos={len(rows)} " + " ".join(rows), flush=True)


peak, running = 0.0, True


def sample():
    global peak
    while running:
        peak = max(peak, memory_mb())
        time.sleep(0.05)


def post(value):
    raw = json.dumps({"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "111"}, **value}}]}]}).encode()
    sig = "sha256=" + hmac.new(b"ficticio", raw, hashlib.sha256).hexdigest()
    request = urllib.request.Request("http://127.0.0.1:8000/webhook", data=raw, headers={
        "Content-Type": "application/json", "X-Hub-Signature-256": sig})
    start = time.time()
    with urllib.request.urlopen(request, timeout=180) as response:
        assert response.status == 200
    return time.time() - start


for _ in range(100):
    try:
        urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=2)
        break
    except Exception:
        time.sleep(0.3)
boot = memory_mb()
breakdown(f"al arranque (total {boot:.0f} MiB)")
time.sleep(3)
idle = memory_mb()
breakdown(f"en reposo (total {idle:.0f} MiB)")
peak = idle
threading.Thread(target=sample, daemon=True).start()
times, count, died = [], 0, ""
ROUTE = ["hola", "4", "asesor", "menu", "2", "Prueba, Negocio Ficticio", "3", "cancelar", "1", "menu"]
try:
    for turn in range(20):                        # 20 recorridos: 200 mensajes y 600 estados
        phone = f"52155500001{turn:02d}"
        for text in ROUTE:
            n = next(ids)
            times.append(post({"messages": [{"from": phone, "id": f"wamid.IN{n}", "type": "text",
                                             "timestamp": str(int(time.time())), "text": {"body": text}}]}))
            for status in ("sent", "delivered", "read"):     # como los manda Meta
                post({"statuses": [{"id": f"wamid.ST{n}", "status": status, "recipient_id": phone,
                                    "timestamp": str(int(time.time()))}]})
            count += 1
            if count in (10, 30, 100):
                print(f"  {LABEL}: memoria tras {count} mensajes = {memory_mb():.0f} MB", flush=True)
except Exception as exc:
    died = f"{type(exc).__name__}: {exc}"
time.sleep(5)
running = False
breakdown(f"al terminar (total {memory_mb():.0f} MiB)")
times.sort()
result = {"version": LABEL, "mensajes": count, "memoria_reposo_MB": round(idle), "memoria_pico_MB": round(peak),
          "memoria_final_MB": round(memory_mb()), "limite_Render_MB": 512,
          "respuesta_mediana_s": round(times[len(times) // 2], 3) if times else None,
          "respuesta_p95_s": round(times[int(len(times) * .95) - 1], 3) if times else None,
          "respuesta_max_s": round(times[-1], 3) if times else None, "error": died}
print("RESULTADO " + json.dumps(result, ensure_ascii=False))
