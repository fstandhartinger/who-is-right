import asyncio, base64, json, os, time
from contextlib import suppress
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from websockets.asyncio.client import connect

MODEL = "gemini-2.5-flash-native-audio-latest"
SESSION_SECONDS = int(os.getenv("SESSION_SECONDS", "180"))
SESSIONS_PER_IP_DAY = int(os.getenv("SESSIONS_PER_IP_DAY", "3"))
GLOBAL_SESSIONS_DAY = int(os.getenv("GLOBAL_SESSIONS_DAY", "100"))
GLOBAL_JEV_CALLS_DAY = int(os.getenv("GLOBAL_JEV_CALLS_DAY", "300"))
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "5"))
JEV_URL = "https://jev-router.app.mintapis.com/v1/systemone"

SYSTEM = """You are the referee in a playful live argument fact-check party demo.
Listen to the speakers. Emit input transcription continuously. When you hear ONE complete,
checkable factual claim, call check_claim exactly once with the claim and at most two short
sentences of relevant context. Never call it for preferences, predictions, sarcasm, feelings,
relationship grievances, or subjective/general claims (for example 'you always leave dishes').
For those, call feeling_not_fact with a short quote and playful, kind explanation. Do not answer
claims yourself. Wait for the tool result, then continue listening. Keep the tone kind: no winner,
no shaming, no medical/legal/financial authority."""

def today(): return date.today().isoformat()

class Limits:
    def __init__(self):
        self.day = today(); self.by_ip = defaultdict(int); self.sessions = 0
        self.jev = 0; self.active = 0; self.lock = asyncio.Lock()
    def reset(self):
        if self.day != today():
            self.day=today(); self.by_ip.clear(); self.sessions=self.jev=0
    async def enter(self, ip):
        async with self.lock:
            self.reset()
            if self.by_ip[ip] >= SESSIONS_PER_IP_DAY: return "You’ve refereed enough today — come back tomorrow."
            if self.sessions >= GLOBAL_SESSIONS_DAY or self.jev >= GLOBAL_JEV_CALLS_DAY: return "The truth budget is tucked in for the night. Come back tomorrow."
            if self.active >= MAX_CONCURRENT: return "The referee is juggling another argument. Try again in a minute."
            self.by_ip[ip]+=1; self.sessions+=1; self.active+=1
    async def leave(self):
        async with self.lock: self.active=max(0,self.active-1)
    async def take_jev(self):
        async with self.lock:
            self.reset()
            if self.jev >= GLOBAL_JEV_CALLS_DAY: return False
            self.jev += 1; return True

limits=Limits()

def client_ip(scope):
    headers={k.decode():v.decode() for k,v in scope.get("headers",[])}
    return headers.get("cf-connecting-ip") or headers.get("x-forwarded-for","").split(",")[0].strip() or scope.get("client",("unknown",))[0]

@asynccontextmanager
async def lifespan(app):
    yield

app=FastAPI(lifespan=lifespan)
PUBLIC=Path(__file__).parent/"public"

@app.get("/health")
async def health(): return {"ok":True,"model":MODEL}

@app.get("/api/config")
async def config():
    return {"model":MODEL,"sessionSeconds":SESSION_SECONDS,"sessionsPerIpDay":SESSIONS_PER_IP_DAY,"privacy":"Audio is processed live and is not recorded or stored."}

@app.get("/")
async def index(): return FileResponse(PUBLIC/"index.html")

@app.get("/app.js")
async def js(): return FileResponse(PUBLIC/"app.js",media_type="text/javascript")

@app.get("/style.css")
async def css(): return FileResponse(PUBLIC/"style.css",media_type="text/css")

async def jev_decision(claim, context):
    if not await limits.take_jev(): return {"limited":True}
    body={"model":"classifier-fast","state":f"Claim: {claim}\nContext: {context}\nJudge only factual truth. If context is insufficient, prefer uncertain.","questions":{"verdict":{"type":"choice","instructions":"Is the claim factually true?","criteria":{"true":"The factual claim is correct.","false":"The factual claim is incorrect."}}}}
    async with httpx.AsyncClient(timeout=8) as client:
        r=await client.post(JEV_URL,json=body); r.raise_for_status(); data=r.json()
    answer=(data.get("answers") or {}).get("verdict") or {}
    if isinstance(answer,str):
        label=answer; probability=.86
    else:
        label=answer.get("answer") or answer.get("choice") or answer.get("value") or "true"
        probability=float(answer.get("probability") or answer.get("confidence") or .72)
    probability=max(.5,min(.99,probability))
    truth_prob=probability if str(label).lower()=="true" else 1-probability
    return {"claim":claim,"truthProbability":round(truth_prob,2),"confidence":round(abs(truth_prob-.5)*2,2),"provider":r.headers.get("x-jev-provider","Jev gateway")}

TOOLS=[{"functionDeclarations":[
 {"name":"check_claim","description":"Check one objective factual claim.","parameters":{"type":"OBJECT","properties":{"claim":{"type":"STRING"},"context":{"type":"STRING"}},"required":["claim","context"]}},
 {"name":"feeling_not_fact","description":"Mark a subjective feeling, taste, or relationship grievance without judging it.","parameters":{"type":"OBJECT","properties":{"claim":{"type":"STRING"},"reason":{"type":"STRING"}},"required":["claim","reason"]}}
]}]

@app.websocket("/ws")
async def live(ws:WebSocket):
    await ws.accept(); ip=client_ip(ws.scope); refusal=await limits.enter(ip)
    if refusal:
        await ws.send_json({"type":"limited","message":refusal}); await ws.close(code=4429); return
    started=time.monotonic()
    if not os.getenv("GOOGLE_API_KEY"):
        await ws.send_json({"type":"error","message":"The referee is off duty. Please try later."}); await limits.leave(); await ws.close(code=1011); return
    try:
      url="wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContent?key="+os.environ["GOOGLE_API_KEY"]
      async with connect(url,max_size=8_000_000) as session:
        await session.send(json.dumps({"setup":{"model":"models/"+MODEL,"generationConfig":{"responseModalities":["AUDIO"]},"systemInstruction":{"parts":[{"text":SYSTEM}]},"tools":TOOLS,"inputAudioTranscription":{}}}))
        setup=json.loads(await session.recv())
        if "setupComplete" not in setup: raise RuntimeError("Gemini setup failed")
        await ws.send_json({"type":"ready","seconds":SESSION_SECONDS,"model":MODEL})
        async def upstream():
          while True:
            if time.monotonic()-started>SESSION_SECONDS: await ws.send_json({"type":"ended","message":"Three minutes! The gavel needs a tiny nap."}); return
            raw=await asyncio.wait_for(ws.receive_text(),timeout=SESSION_SECONDS)
            msg=json.loads(raw)
            if msg.get("type")=="audio":
              await session.send(json.dumps({"realtimeInput":{"audio":{"data":msg["data"],"mimeType":"audio/pcm;rate=16000"}}}))
            elif msg.get("type")=="end":
              await session.send(json.dumps({"realtimeInput":{"audioStreamEnd":True}})); return
        async def downstream():
          async for raw in session:
            response=json.loads(raw)
            content=response.get("serverContent") or {}
            transcription=content.get("inputTranscription") or {}
            if transcription.get("text"): await ws.send_json({"type":"transcript","text":transcription["text"]})
            tc=response.get("toolCall")
            if tc:
              replies=[]
              for call in tc.get("functionCalls",[]):
                args=call.get("args") or {}; name=call.get("name")
                if name=="feeling_not_fact":
                  result={"kind":"feeling","claim":args.get("claim","That"),"reason":args.get("reason","That’s a feeling, not a lab result.")}
                  await ws.send_json({"type":"feeling",**result})
                else:
                  try: result=await jev_decision(args.get("claim",""),args.get("context",""))
                  except Exception: result={"error":"The tiny truth machine shrugged. Try the next claim."}
                  if result.get("limited"): await ws.send_json({"type":"limited","message":"The truth budget is tucked in for the night. Come back tomorrow."})
                  elif result.get("error"): await ws.send_json({"type":"error","message":result["error"]})
                  else: await ws.send_json({"type":"verdict",**result})
                replies.append({"id":call.get("id"),"name":name,"response":result})
              await session.send(json.dumps({"toolResponse":{"functionResponses":replies}}))
        tasks=[asyncio.create_task(upstream()),asyncio.create_task(downstream())]
        done,pending=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
        for t in pending: t.cancel()
        for t in tasks:
          with suppress(asyncio.CancelledError, WebSocketDisconnect): await t
    except (WebSocketDisconnect,asyncio.TimeoutError): pass
    except Exception:
      try: await ws.send_json({"type":"error","message":"The referee dropped the gavel. Please try again."})
      except Exception: pass
    finally:
      await limits.leave()

@app.get("/{path:path}")
async def static(path:str):
    p=(PUBLIC/path).resolve()
    if PUBLIC.resolve() not in p.parents: return JSONResponse({"detail":"not found"},404)
    return FileResponse(p) if p.is_file() else JSONResponse({"detail":"not found"},404)
