"""Live Fact Check — streams 16 kHz mono PCM from the browser, classifies the last 3 s every 200 ms.

Every tick (TICK_MS) the server sends the latest WINDOW_S seconds as WAV to OpenRouter IF the window
contains voice (RMS gate) and fewer than MAX_INFLIGHT requests are open for this session. Results go back
to the browser together with a server-side smoothed verdict (EMA + hysteresis, see smoother.py), so the
UI and the eval harness see exactly the same verdict. Audio is never written to disk.
"""
import asyncio, hmac, json, logging, os, time
from collections import defaultdict
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

import model as M
from smoother import Smoother

MODEL = os.getenv("MODEL", "google/gemini-3.1-flash-lite")
THINKING = os.getenv("THINKING_LEVEL", "minimal")
SR = 16000
WINDOW_S = float(os.getenv("WINDOW_S", "3.0"))
TICK_MS = int(os.getenv("TICK_MS", "200"))
MAX_INFLIGHT = int(os.getenv("MAX_INFLIGHT", "12"))
MAX_FRAME_BYTES = int(os.getenv("MAX_FRAME_BYTES", "64000"))
SESSION_SECONDS = int(os.getenv("SESSION_SECONDS", "60"))
SESSION_CALLS_MAX = int(os.getenv("SESSION_CALLS_MAX", "150"))
IDLE_SECONDS = int(os.getenv("IDLE_SECONDS", "25"))
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "4"))
SESSIONS_PER_IP_DAY = int(os.getenv("SESSIONS_PER_IP_DAY", "3"))
GLOBAL_CALLS_DAY = int(os.getenv("GLOBAL_CALLS_DAY", "2000"))
VAD_MIN_RMS = float(os.getenv("VAD_MIN_RMS", "250"))  # int16 units, ~ -42 dBFS
EVAL_TOKEN = os.getenv("EVAL_TOKEN", "")  # optional: bypasses the per-IP session cap only

log = logging.getLogger("lfc")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def utc_day():
    return datetime.now(timezone.utc).date().isoformat()


class Limits:
    def __init__(self):
        self.day = utc_day(); self.by_ip = defaultdict(int); self.calls = 0; self.active = 0
        self.sessions = 0; self.lock = asyncio.Lock()

    def _roll(self):
        if self.day != utc_day():
            self.day = utc_day(); self.by_ip.clear(); self.calls = 0; self.sessions = 0

    async def enter(self, ip, privileged=False):
        async with self.lock:
            self._roll()
            if self.calls >= GLOBAL_CALLS_DAY:
                return "daily", "Today's fact-checking budget is used up. Come back tomorrow!"
            if not privileged and self.by_ip[ip] >= SESSIONS_PER_IP_DAY:
                return "ip", f"You've had your {SESSIONS_PER_IP_DAY} sessions for today. Come back tomorrow!"
            if self.active >= MAX_CONCURRENT:
                return "busy", "All listening slots are busy right now. Try again in a minute."
            self.by_ip[ip] += 1; self.active += 1; self.sessions += 1
            return None

    async def leave(self):
        async with self.lock:
            self.active = max(0, self.active - 1)

    def take_call(self):  # single event loop -> no await between check and increment
        self._roll()
        if self.calls >= GLOBAL_CALLS_DAY:
            return False
        self.calls += 1
        return True


limits = Limits()
client: httpx.AsyncClient | None = None


def has_provider_key():
    return bool(os.getenv("OPENROUTER_API_KEY") or os.getenv("OPEN_ROUTER_API_KEY"))


@asynccontextmanager
async def lifespan(app):
    global client
    client = httpx.AsyncClient(limits=httpx.Limits(max_connections=64, max_keepalive_connections=32))
    yield
    await client.aclose()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
PUBLIC = Path(__file__).parent / "public"


def client_ip(scope):
    h = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
    return (h.get("cf-connecting-ip") or h.get("x-forwarded-for", "").split(",")[0].strip()
            or (scope.get("client") or ("unknown",))[0])


@app.get("/health")
async def health():
    return {"ok": True, "model": MODEL, "key": has_provider_key(), "active": limits.active,
            "calls_today": limits.calls, "calls_cap": GLOBAL_CALLS_DAY}


@app.get("/api/config")
async def config():
    samples = json.loads((PUBLIC / "samples" / "samples.json").read_text())
    return {"model": MODEL, "sessionSeconds": SESSION_SECONDS, "sessionCallsMax": SESSION_CALLS_MAX,
            "sessionsPerIpDay": SESSIONS_PER_IP_DAY, "tickMs": TICK_MS, "windowS": WINDOW_S, "samples": samples,
            "benchmarkUrl": "https://benchmarkheaven.com/audio-jev-bench"}


def voiced(pcm: bytes) -> bool:
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    n = len(x) // 320  # 20 ms frames
    if n < 5:
        return False
    rms = np.sqrt((x[: n * 320].reshape(n, 320) ** 2).mean(axis=1))
    thr = max(VAD_MIN_RMS, 3.0 * float(np.percentile(rms, 10)))
    return int((rms > thr).sum()) >= 8  # >= 160 ms of voice


class Session:
    def __init__(self, ws: WebSocket):
        self.ws = ws; self.buf = bytearray(); self.samples = 0; self.last_audio = time.monotonic()
        self.created = self.last_audio
        self.started = None; self.inflight = 0; self.tick = 0; self.last_tick_samples = -1
        self.smoother = Smoother(); self.calls = 0; self.errors = 0; self.closed = False
        self.send_lock = asyncio.Lock(); self.tasks = set()

    @property
    def t(self):
        return self.samples / SR

    async def send(self, msg):
        if self.closed:
            return
        async with self.send_lock:
            with suppress(Exception):
                await self.ws.send_text(json.dumps(msg, separators=(",", ":")))

    def add_audio(self, data: bytes):
        if len(data) % 2:
            data = data[:-1]
        if self.started is None:
            self.started = time.monotonic()
        self.buf += data; self.samples += len(data) // 2; self.last_audio = time.monotonic()
        cap = int(WINDOW_S * SR) * 2
        if len(self.buf) > cap:
            del self.buf[: len(self.buf) - cap]

    async def classify(self, tick, t_end, pcm):
        try:
            r = await M.classify(client, pcm, MODEL, THINKING)
            self.errors = 0
            label = r["label"]
            verdict = self.smoother.update(tick, label)
            await self.send({"type": "result", "tick": tick, "t": round(t_end, 3), "label": label,
                             "agreement": verdict["agreement"],
                             "latency_ms": r["latency_ms"], "recv_t": round(self.t, 3), "verdict": verdict})
        except Exception as e:  # network / parse / API error: report the tick as failed, keep going
            self.errors += 1
            log.warning("model error: %s", str(e)[:160])
            await self.send({"type": "tick", "tick": tick, "t": round(t_end, 3), "skipped": "error"})
            if self.errors == 6:
                await self.send({"type": "error", "message": "The model is not answering right now. Please try again later."})
        finally:
            self.inflight -= 1

    async def ticker(self):
        while not self.closed:
            await asyncio.sleep(TICK_MS / 1000)
            if self.started is None:
                if time.monotonic() - self.created > 15:
                    await self.send({"type": "ended", "reason": "idle", "message": "No audio arrived. Tap to start again."})
                    return
                continue
            if time.monotonic() - self.last_audio > IDLE_SECONDS:
                await self.send({"type": "ended", "reason": "idle", "message": "Stopped after a quiet while. Tap to listen again."})
                return
            if self.samples == self.last_tick_samples:
                continue  # stream paused: no new audio since the last tick
            self.last_tick_samples = self.samples
            if self.t > SESSION_SECONDS:
                await self.send({"type": "ended", "reason": "time",
                                 "message": f"That's {SESSION_SECONDS} seconds of listening — the maximum per session."})
                return
            if self.calls >= SESSION_CALLS_MAX:
                await self.send({"type": "ended", "reason": "call_limit",
                                 "message": f"That's {SESSION_CALLS_MAX} checks — the maximum per session."})
                return
            self.tick += 1
            tick, t_end, pcm = self.tick, self.t, bytes(self.buf)
            if len(pcm) < int(WINDOW_S * SR) * 2:
                await self.send({"type": "tick", "tick": tick, "t": round(t_end, 3), "skipped": "warming"})
                continue  # Every provider call receives a full three-second window.
            if not voiced(pcm):
                v = self.smoother.silence(tick)
                await self.send({"type": "tick", "tick": tick, "t": round(t_end, 3), "skipped": "silence", "verdict": v})
                continue
            if self.inflight >= MAX_INFLIGHT:
                await self.send({"type": "tick", "tick": tick, "t": round(t_end, 3), "skipped": "busy"})
                continue
            if not limits.take_call():
                await self.send({"type": "limited", "reason": "daily",
                                 "message": "Today's fact-checking budget is used up. Come back tomorrow!"})
                return
            self.inflight += 1; self.calls += 1
            task = asyncio.create_task(self.classify(tick, t_end, pcm))
            self.tasks.add(task); task.add_done_callback(self.tasks.discard)
            await self.send({"type": "sent", "tick": tick, "t": round(t_end, 3), "inflight": self.inflight})


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    ip = client_ip(ws.scope)
    tok = ws.query_params.get("key", "")
    privileged = bool(EVAL_TOKEN) and hmac.compare_digest(tok, EVAL_TOKEN)
    if not has_provider_key():
        await ws.send_json({"type": "error", "message": "The fact checker is off duty. Please try later."})
        await ws.close(code=1011); return
    refusal = await limits.enter(ip, privileged)
    if refusal:
        reason, message = refusal
        await ws.send_json({"type": "limited", "reason": reason, "message": message})
        await ws.close(code=4429); return
    s = Session(ws)
    await ws.send_json({"type": "ready", "model": MODEL, "seconds": SESSION_SECONDS,
                        "callsMax": SESSION_CALLS_MAX, "tickMs": TICK_MS, "windowS": WINDOW_S})
    ticker = asyncio.create_task(s.ticker())
    log.info("session start active=%d calls_today=%d", limits.active, limits.calls)
    try:
        while True:
            recv = asyncio.create_task(ws.receive())
            done, _ = await asyncio.wait({recv, ticker}, return_when=asyncio.FIRST_COMPLETED)
            if ticker in done:
                recv.cancel(); break
            msg = recv.result()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes"):
                if len(msg["bytes"]) > MAX_FRAME_BYTES:
                    await ws.close(code=1009, reason="audio frame too large")
                    break
                s.add_audio(msg["bytes"])
            elif msg.get("text"):
                with suppress(Exception):
                    m = json.loads(msg["text"])
                    if m.get("type") == "stop":
                        break
                    if m.get("type") == "reset":
                        s.smoother.reset(); s.buf.clear()
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        # let in-flight results drain briefly so the last verdict still reaches the client
        t0 = time.monotonic()
        while s.inflight and time.monotonic() - t0 < 4:
            await asyncio.sleep(0.05)
        s.closed = True
        ticker.cancel()
        for t in list(s.tasks):
            t.cancel()
        await limits.leave()
        log.info("session end audio_s=%.1f calls=%d active=%d calls_today=%d", s.t, s.calls, limits.active, limits.calls)
        with suppress(Exception):
            await ws.close()


@app.get("/")
async def index():
    return FileResponse(PUBLIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/{path:path}")
async def static(path: str):
    p = (PUBLIC / path).resolve()
    if PUBLIC.resolve() not in p.parents or not p.is_file():
        return JSONResponse({"detail": "not found"}, 404)
    return FileResponse(p, headers={"Cache-Control": "public, max-age=300"})
