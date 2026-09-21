import asyncio, base64, hashlib, hmac, json, os, re, time, uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager, suppress
from datetime import date
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from websockets.asyncio.client import connect

LIVE_MODEL = "gemini-3.8-live"
JEV_MODEL = "jev-latest"
JEV_URL = "https://api.typesafe.ai/v1/systemone"
SERPER_URL = "https://google.serper.dev/search"
SESSION_SECONDS = int(os.getenv("SESSION_SECONDS", "180"))
SESSIONS_PER_IP_DAY = int(os.getenv("SESSIONS_PER_IP_DAY", "3"))
GLOBAL_SESSIONS_DAY = int(os.getenv("GLOBAL_SESSIONS_DAY", "80"))
GLOBAL_JEV_CALLS_DAY = int(os.getenv("GLOBAL_JEV_CALLS_DAY", "240"))
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "5"))
SEARCHES_PER_SESSION = int(os.getenv("SEARCHES_PER_SESSION", "8"))
SEARCHES_PER_DAY = int(os.getenv("SEARCHES_PER_DAY", "160"))
UTTERANCE_PAUSE_SECONDS = float(os.getenv("UTTERANCE_PAUSE_SECONDS", "2.40"))
PUNCTUATION_SETTLE_SECONDS = float(os.getenv("PUNCTUATION_SETTLE_SECONDS", "0.55"))
ROLLING_SECONDS = 30
DEBUG_RETENTION_SECONDS = 48 * 3600
DEBUG_DIR = Path(os.getenv("DEBUG_LOG_DIR", "/tmp/who-is-right-debug"))
TYPESAFE_API_KEY = os.getenv("TYPESAFE_API_KEY", "")
SERPER_API_KEY = os.getenv("SERPER_API_KEY", "") or os.getenv("SERPER_DEV_API_KEY", "")
DEBUG_TOKEN = os.getenv("DEBUG_TOKEN", "")

LIVE_SYSTEM = """You are the referee in a playful live argument fact-check demo.
Listen continuously and transcribe faithfully. Wait for a complete statement across audio chunks.
When you hear one complete, objective, externally checkable factual claim, call check_claim
exactly once with the full self-contained claim and your best guess whether a web check is
needed. Ignore opinions, feelings, preferences, predictions, sarcasm, rhetorical remarks,
relationship grievances, and incomplete fragments. Do not fact-check the claim yourself.
Wait for the tool response, then say only its short comic_line. Continue listening afterward.
Keep it kind: no winner, no shaming, and no medical, legal, or financial authority."""

CHECK_CLAIM_TOOL={"functionDeclarations":[{
    "name":"check_claim","description":"Check one complete objective factual claim.","behavior":"BLOCKING",
    "parameters":{"type":"OBJECT","properties":{
        "claim":{"type":"STRING","description":"The complete self-contained factual claim."},
        "needs_web_check":{"type":"BOOLEAN","description":"Whether external web evidence is probably required."}
    },"required":["claim","needs_web_check"]}
}]}

def today(): return date.today().isoformat()
def now_ms(): return int(time.time() * 1000)

class Limits:
    def __init__(self):
        self.day = today(); self.by_ip = defaultdict(int); self.sessions = 0
        self.jev = self.searches = self.active = 0; self.lock = asyncio.Lock()
    def reset(self):
        if self.day != today():
            self.day=today(); self.by_ip.clear(); self.sessions=self.jev=self.searches=0
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
    async def take_search(self):
        async with self.lock:
            self.reset()
            if self.searches >= SEARCHES_PER_DAY: return False
            self.searches += 1; return True

limits=Limits()

def client_ip(scope):
    headers={k.decode():v.decode() for k,v in scope.get("headers",[])}
    return headers.get("cf-connecting-ip") or headers.get("x-forwarded-for","").split(",")[0].strip() or scope.get("client",("unknown",))[0]

def debug_authorized(token):
    return bool(DEBUG_TOKEN and token and hmac.compare_digest(token, DEBUG_TOKEN))

def clean_old_logs():
    if not DEBUG_DIR.exists(): return
    cutoff=time.time()-DEBUG_RETENTION_SECONDS
    for p in DEBUG_DIR.glob("*.jsonl"):
        with suppress(OSError):
            if p.stat().st_mtime < cutoff: p.unlink()

class DebugLog:
    def __init__(self, enabled):
        self.enabled=enabled; self.session_id=uuid.uuid4().hex[:12]; self.started=now_ms()
        self.path=DEBUG_DIR/f"{self.started}-{self.session_id}.jsonl"
        if enabled: DEBUG_DIR.mkdir(parents=True,exist_ok=True); clean_old_logs()
    def write(self,event,**data):
        record={"timestamp":now_ms(),"sessionId":self.session_id,"event":event,**data}
        if self.enabled:
            with self.path.open("a",encoding="utf-8") as f: f.write(json.dumps(record,ensure_ascii=False,separators=(",",":"))+"\n")
        return record

class UtteranceAssembler:
    def __init__(self, rolling_seconds=ROLLING_SECONDS):
        self.pending=[]; self.history=deque(); self.last_chunk_at=0.; self.rolling_seconds=rolling_seconds
    def add(self,text,at=None):
        text=re.sub(r"[\r\n\t]+"," ",text or "")
        if not text.strip(): return
        at=at or time.monotonic(); self.pending.append(text); self.last_chunk_at=at
        self.history.append((at,text))
        while self.history and at-self.history[0][0] > self.rolling_seconds: self.history.popleft()
    def ready(self,at=None):
        if not self.pending: return False
        elapsed=(at or time.monotonic())-self.last_chunk_at
        joined="".join(self.pending).strip()
        # Gemini often pauses for 1-2 seconds mid-thought and can even emit a
        # connector as a standalone delta. Punctuation is a strong boundary;
        # otherwise wait for a genuinely conversational pause.
        if joined.lower() in {"and","but","or","so","because"}: return False
        punctuated=bool(re.search(r"[.!?][\"'”]?$",joined))
        return (punctuated and elapsed >= PUNCTUATION_SETTLE_SECONDS) or elapsed >= UTTERANCE_PAUSE_SECONDS
    def flush(self):
        text="".join(self.pending); self.pending=[]
        text=re.sub(r"\s+([,.;!?])",r"\1",text); text=re.sub(r"([,.;!?])(\w)",r"\1 \2",text)
        return re.sub(r"\s+"," ",text).strip()
    def context(self): return re.sub(r"\s+"," ","".join(x[1] for x in self.history)).strip()

def claim_candidates(utterance):
    # Preserve whole clauses; Jev rejects hedges/opinions. Pronouns are resolved by
    # supplying the complete utterance and rolling context to every judgment.
    pieces=re.split(r"(?<=[.!?])\s+|\s*(?:;|\b(?:but|and)\b)\s*|,\s*(?=(?:I|you|he|she|it|we|they|there)\b)",utterance,flags=re.I)
    candidates=[p.strip(" ,.;\"'“”") for p in pieces if len(p.strip(" ,.;\"'“”").split()) >= 3]
    # Do not spend a Jev call on an obviously subjectless tail such as the
    # observed "are driving on the street". Semantic ambiguity still goes to
    # Jev; this catches only fragments beginning with an auxiliary.
    candidates=[c for c in candidates if not re.match(r"^(?:am|are|is|was|were|be|been|being|has|have|had|do|does|did|can|could|may|might|must|shall|should|will|would)\b",c,re.I)]
    candidates=[re.sub(r"^(?:i am|i'm) sure,?\s+","",c,flags=re.I) for c in candidates]
    antecedent=None
    if candidates:
        match=re.match(r"^(.+?)\s+(?:is|was|has|had|does|did|can|will)\b",candidates[0],re.I)
        if match and 1 <= len(match.group(1).split()) <= 8: antecedent=match.group(1)
    if antecedent: candidates=[re.sub(r"^it\b",antecedent,c,flags=re.I) for c in candidates]
    return candidates

async def jev(body, log, purpose):
    if not await limits.take_jev(): return None, {"limited":True}
    started=time.monotonic(); log.write("jev_request",purpose=purpose,request=body)
    async with httpx.AsyncClient(timeout=12) as client:
        r=await client.post(JEV_URL,json=body,headers={"Authorization":f"Bearer {TYPESAFE_API_KEY}"}); r.raise_for_status(); data=r.json()
    elapsed=round((time.monotonic()-started)*1000)
    usage=data.get("usage") or {}; cost=round(float(usage.get("input_tokens",0))*.042/1_000_000,8)
    log.write("jev_response",purpose=purpose,response=data,timingMs=elapsed,costUsd=cost)
    return data,{"timingMs":elapsed,"costUsd":cost,"model":data.get("model",JEV_MODEL),"usage":usage}

async def search_web(query, log):
    started=time.monotonic(); log.write("web_search_request",query=query)
    async with httpx.AsyncClient(timeout=10) as client:
        r=await client.post(SERPER_URL,json={"q":query,"num":5},headers={"X-API-KEY":SERPER_API_KEY,"Content-Type":"application/json"}); r.raise_for_status(); data=r.json()
    results=[{"title":x.get("title","")[:180],"snippet":x.get("snippet","")[:500],"source":x.get("link","")[:400]} for x in (data.get("organic") or [])[:5]]
    elapsed=round((time.monotonic()-started)*1000); log.write("web_search_response",query=query,results=results,timingMs=elapsed)
    return results,elapsed

async def evaluate_candidate(candidate, utterance, rolling_context, log, session_searches, gemini_web_hint=None):
    state={"candidate":candidate,"assembled_utterance":utterance,"recent_transcript_context":rolling_context}
    triage_body={"model":JEV_MODEL,"state":state,"questions":{
        "is_checkable":{"type":"noul","instructions":"Is `candidate` a complete, objective, externally verifiable factual claim? Reject fragments, opinions, feelings, predictions, rhetorical remarks, and mere confidence phrases.","criteria":{"true":"A complete factual proposition with enough meaning to check.","false":"Not a factual claim or too fragmentary to check."}},
        "needs_web":{"type":"noul","instructions":"If `candidate` is a checkable factual claim, does verifying its truth require current or external web evidence rather than only interpreting the wording?","criteria":{"true":"External evidence or current facts are needed.","false":"No web lookup is appropriate, or the candidate is not checkable."}}
    }}
    triage,tm=await jev(triage_body,log,"claim_triage")
    if not triage: return {"limited":True}
    answers=triage.get("answers") or {}; check_p=float((answers.get("is_checkable") or {}).get("noul",0)); web_p=float((answers.get("needs_web") or {}).get("noul",0))
    debug={"assembledUtterance":utterance,"claim":candidate,"checkableProbability":check_p,"webCheckProbability":web_p,"webCheckNeeded":False,"query":None,"results":[],"jevQuestions":triage_body["questions"],"jevOutput":answers,"timings":{"triageMs":tm["timingMs"]},"costUsd":tm["costUsd"],"model":tm["model"]}
    log.write("claim_candidate",candidate=candidate,utterance=utterance,checkableProbability=check_p,webCheckProbability=web_p,geminiWebHint=gemini_web_hint)
    if check_p < .50: return {"ignored":True,"debug":debug}
    results=[]; query=None
    if web_p >= .55 and session_searches[0] < SEARCHES_PER_SESSION and SERPER_API_KEY and await limits.take_search():
        query=re.sub(r"[^\w\s'\-]"," ",candidate); query=re.sub(r"\s+"," ",query).strip()[:160]
        log.write("tool_call",tool="serper_search",arguments={"query":query}); results,search_ms=await search_web(query,log); session_searches[0]+=1
        debug.update(webCheckNeeded=True,query=query,results=results); debug["timings"]["searchMs"]=search_ms
    evidence_section=f"Web search results for query: {query}\n"+"\n".join(f"- {x['title']} — {x['snippet']} ({x['source']})" for x in results) if results else "Web search: not requested by the verification triage."
    verdict_state={**state,"web_evidence":evidence_section}
    question={"type":"choice","instructions":"Given `candidate`, its full utterance/context, and the clearly labelled web evidence when present, is the factual claim true, false, or not verifiable from the available evidence? Do not treat the speaker's confidence as evidence.","criteria":{"true":"Evidence supports the claim.","false":"Evidence contradicts the claim.","uncertain":"Evidence is insufficient, mixed, or the claim remains ambiguous."}}
    body={"model":JEV_MODEL,"state":verdict_state,"questions":{"verdict":question}}
    verdict,vm=await jev(body,log,"fact_verdict"); answer=(verdict.get("answers") or {}).get("verdict") or {}
    probs=answer.get("probabilities") or {}; label=answer.get("choice","uncertain"); truth_prob=float(probs.get("true",0)); confidence=float(answer.get("confidence",0))
    debug["jevQuestions"]={"triage":triage_body["questions"],"verdict":question}; debug["jevOutput"]={"triage":answers,"verdict":answer}; debug["timings"]["verdictMs"]=vm["timingMs"]; debug["costUsd"]=round(debug["costUsd"]+vm["costUsd"],8); debug["model"]=vm["model"]
    log.write("tool_call",tool="publish_verdict",arguments={"claim":candidate,"label":label})
    return {"claim":candidate,"truthProbability":round(truth_prob,2),"confidence":round(confidence,2),"label":label,"provider":vm["model"],"debug":debug}

async def process_utterance(utterance, context, ws, log, session_searches):
    log.write("utterance_assembled",utterance=utterance,rollingContext=context)
    await ws.send_json({"type":"utterance","text":utterance})
    candidates=claim_candidates(utterance)
    log.write("tool_call",tool="extract_claim_candidates",arguments={"utterance":utterance},result=candidates)
    emitted=False
    for candidate in candidates[:4]:
        try: result=await evaluate_candidate(candidate,utterance,context,log,session_searches)
        except Exception as exc:
            log.write("pipeline_error",stage="evaluate_candidate",error=type(exc).__name__); continue
        if result.get("limited"): await ws.send_json({"type":"limited","message":"The truth budget is tucked in for the night. Come back tomorrow."}); return
        await ws.send_json({"type":"debug_claim",**result["debug"]})
        if not result.get("ignored"):
            await ws.send_json({"type":"verdict",**{k:v for k,v in result.items() if k!="debug"}}); emitted=True
    if not emitted: await ws.send_json({"type":"no_claim","text":utterance})

def comic_line(result):
    if result.get("label")=="true": return "Ding ding — that claim survives the truth ray!"
    if result.get("label")=="false": return "Plot twist: the facts just pulled the emergency brake!"
    return "The evidence fog is too thick — no victory lap yet!"

async def execute_check_claim(args, utterance, context, ws, log, session_searches, claim_cache):
    claim=re.sub(r"\s+"," ",str(args.get("claim") or "")).strip()[:500]
    hint=args.get("needs_web_check")
    log.write("gemini_tool_call",tool="check_claim",arguments={"claim":claim,"needs_web_check":hint})
    await ws.send_json({"type":"debug_trace","stage":"Gemini called check_claim","arguments":{"claim":claim,"needs_web_check":hint}})
    cache_key=re.sub(r"[^\w]+"," ",claim.lower()).strip()
    if cache_key in claim_cache:
        response=claim_cache[cache_key]
        log.write("gemini_tool_response",tool="check_claim",response=response,deduplicated=True)
        await ws.send_json({"type":"debug_trace","stage":"Duplicate tool call reused","response":response})
        return response
    if len(claim.split()) < 3:
        result={"ignored":True,"reason":"Incomplete claim","comic_line":"That sentence needs its other half before the truth ray fires!"}
    else:
        try: result=await evaluate_candidate(claim,utterance or claim,context or utterance or claim,log,session_searches,hint)
        except Exception as exc:
            log.write("pipeline_error",stage="check_claim",error=type(exc).__name__)
            result={"error":"The tiny truth machine shrugged. Try the next claim.","comic_line":"The truth machine dropped its monocle — try the next claim!"}
    if result.get("limited"):
        await ws.send_json({"type":"limited","message":"The truth budget is tucked in for the night. Come back tomorrow."})
        response={"status":"limited","comic_line":"The truth budget is tucked in for the night!"}
    elif result.get("ignored"):
        if result.get("debug"): await ws.send_json({"type":"debug_claim",**result["debug"]})
        await ws.send_json({"type":"no_claim","text":claim})
        response={"status":"ignored","reason":result.get("reason","Jev did not find a complete checkable claim."),"comic_line":result.get("comic_line","That one's a thought, not a testable fact!")}
    elif result.get("error"):
        await ws.send_json({"type":"error","message":result["error"]}); response=result
    else:
        await ws.send_json({"type":"debug_claim",**result["debug"]})
        await ws.send_json({"type":"verdict",**{k:v for k,v in result.items() if k!="debug"}})
        response={k:v for k,v in result.items() if k!="debug"}; response["comic_line"]=comic_line(result)
    log.write("gemini_tool_response",tool="check_claim",response=response)
    await ws.send_json({"type":"debug_trace","stage":"Backend returned tool response","response":response})
    if cache_key: claim_cache[cache_key]=response
    return response

@asynccontextmanager
async def lifespan(app):
    clean_old_logs(); yield

app=FastAPI(lifespan=lifespan); PUBLIC=Path(__file__).parent/"public"

@app.get("/health")
async def health(): return {"ok":True,"liveModel":LIVE_MODEL,"jevModel":JEV_MODEL}

@app.get("/api/config")
async def config(request:Request,response:Response):
    token=request.headers.get("x-debug-token",""); debug=debug_authorized(token)
    if debug: response.set_cookie("wir_debug",token,max_age=10800,secure=request.url.scheme=="https",httponly=True,samesite="strict")
    return {"model":LIVE_MODEL,"jevModel":"jev-1.13.0","sessionSeconds":SESSION_SECONDS,"sessionsPerIpDay":SESSIONS_PER_IP_DAY,"debug":debug,"privacy":"Audio is processed live and is not recorded or stored. Debug sessions retain diagnostic transcripts for up to 48 hours."}

@app.get("/")
async def index(): return FileResponse(PUBLIC/"index.html")
@app.get("/app.js")
async def js(): return FileResponse(PUBLIC/"app.js",media_type="text/javascript")
@app.get("/style.css")
async def css(): return FileResponse(PUBLIC/"style.css",media_type="text/css")

@app.get("/api/debug/logs")
async def debug_logs(request:Request):
    if not debug_authorized(request.headers.get("x-debug-token","") or request.cookies.get("wir_debug","")): return JSONResponse({"detail":"not found"},404)
    clean_old_logs(); rows=[]
    for p in sorted(DEBUG_DIR.glob("*.jsonl"),reverse=True)[:20]:
        with suppress(Exception): rows.append({"file":p.name,"events":[json.loads(x) for x in p.read_text().splitlines()]})
    return {"retentionHours":48,"sessions":rows}

@app.websocket("/ws")
async def live(ws:WebSocket):
    await ws.accept(); log=DebugLog(debug_authorized(ws.cookies.get("wir_debug",""))); ip=client_ip(ws.scope); refusal=await limits.enter(ip)
    if refusal: await ws.send_json({"type":"limited","message":refusal}); await ws.close(code=4429); return
    started=time.monotonic(); assembler=UtteranceAssembler(); session_searches=[0]; queue=asyncio.Queue()
    latest_utterance=[""]; tool_calls=[0]; fallback_tasks=[]; claim_cache={}
    log.write("session_started",debug=True)
    if not os.getenv("GOOGLE_API_KEY") or not TYPESAFE_API_KEY:
        await ws.send_json({"type":"error","message":"The referee is off duty. Please try later."}); await limits.leave(); await ws.close(code=1011); return
    try:
      url="wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContent?key="+os.environ["GOOGLE_API_KEY"]
      async with connect(url,max_size=8_000_000) as session:
        setup_body={"setup":{"model":"models/"+LIVE_MODEL,"generationConfig":{"responseModalities":["AUDIO"]},"systemInstruction":{"parts":[{"text":LIVE_SYSTEM}]},"tools":[CHECK_CLAIM_TOOL],"inputAudioTranscription":{},"outputAudioTranscription":{}}}
        log.write("tool_call",tool="gemini_live_setup",arguments={"model":LIVE_MODEL}); await session.send(json.dumps(setup_body)); setup=json.loads(await session.recv())
        if "setupComplete" not in setup: raise RuntimeError("Gemini setup failed")
        await ws.send_json({"type":"ready","seconds":SESSION_SECONDS,"model":LIVE_MODEL,"debug":log.enabled,"sessionId":log.session_id})
        async def upstream():
          while True:
            if time.monotonic()-started>SESSION_SECONDS: await ws.send_json({"type":"ended","message":"Three minutes! The gavel needs a tiny nap."}); return
            raw=await asyncio.wait_for(ws.receive_text(),timeout=SESSION_SECONDS); msg=json.loads(raw)
            if msg.get("type")=="audio": await session.send(json.dumps({"realtimeInput":{"audio":{"data":msg["data"],"mimeType":"audio/pcm;rate=16000"}}}))
            elif msg.get("type")=="debug_transcript" and log.enabled: await queue.put(msg.get("text",""))
            elif msg.get("type")=="end": await session.send(json.dumps({"realtimeInput":{"audioStreamEnd":True}})); return
        async def downstream():
          async for raw in session:
            response=json.loads(raw); server=response.get("serverContent") or {}; transcription=server.get("inputTranscription") or {}
            if transcription.get("text"): await queue.put(transcription["text"])
            for part in (server.get("modelTurn") or {}).get("parts") or []:
                inline=part.get("inlineData") or {}
                if inline.get("data") and str(inline.get("mimeType","")).startswith("audio/pcm"):
                    await ws.send_json({"type":"audio","data":inline["data"],"mimeType":inline.get("mimeType","audio/pcm;rate=24000")})
            output=(server.get("outputTranscription") or {}).get("text")
            if output: log.write("gemini_comic_line",text=output); await ws.send_json({"type":"comic_line","text":output})
            for call in (response.get("toolCall") or {}).get("functionCalls") or []:
                if call.get("name")!="check_claim": continue
                tool_calls[0]+=1; args=call.get("args") or {}
                result=await execute_check_claim(args,latest_utterance[0],assembler.context(),ws,log,session_searches,claim_cache)
                await session.send(json.dumps({"toolResponse":{"functionResponses":[{"id":call.get("id"),"name":"check_claim","response":{"result":result}}]}}))
        async def fallback(utterance,context,call_count):
          await asyncio.sleep(1.6)
          if tool_calls[0]==call_count==0:
            log.write("assembly_backstop",utterance=utterance)
            await process_utterance(utterance,context,ws,log,session_searches)
        async def assemble():
          while True:
            try: text=await asyncio.wait_for(queue.get(),timeout=.2); assembler.add(text); log.write("raw_transcript_chunk",text=text); await ws.send_json({"type":"transcript","text":text})
            except asyncio.TimeoutError: pass
            if assembler.ready():
                utterance=assembler.flush(); context=assembler.context(); latest_utterance[0]=utterance
                log.write("utterance_assembled",utterance=utterance,rollingContext=context); await ws.send_json({"type":"utterance","text":utterance})
                fallback_tasks.append(asyncio.create_task(fallback(utterance,context,tool_calls[0])))
        tasks=[asyncio.create_task(upstream()),asyncio.create_task(downstream()),asyncio.create_task(assemble())]
        done,pending=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
        if assembler.pending:
            latest_utterance[0]=assembler.flush(); log.write("utterance_assembled",utterance=latest_utterance[0],rollingContext=assembler.context())
        for t in pending: t.cancel()
        for t in fallback_tasks: t.cancel()
        for t in tasks:
          with suppress(asyncio.CancelledError,WebSocketDisconnect): await t
    except (WebSocketDisconnect,asyncio.TimeoutError): pass
    except Exception as exc:
      log.write("session_error",error=type(exc).__name__)
      with suppress(Exception): await ws.send_json({"type":"error","message":"The referee dropped the gavel. Please try again."})
    finally:
      log.write("session_ended",searches=session_searches[0]); await limits.leave()

@app.get("/{path:path}")
async def static(path:str):
    p=(PUBLIC/path).resolve()
    if PUBLIC.resolve() not in p.parents: return JSONResponse({"detail":"not found"},404)
    return FileResponse(p) if p.is_file() else JSONResponse({"detail":"not found"},404)
