"""OpenRouter audio call: send a three-second WAV and accept exactly one A/B/C label."""
import base64
import io
import json
import os
import time
import wave

import httpx

URL = "https://openrouter.ai/api/v1/chat/completions"

PROMPT = """You are a live audio fact-check labeler. You receive the latest 3 seconds of a spoken audio stream.
Judge only the latest complete spoken statement that can be understood from the audio.
Return exactly one uppercase letter and nothing else:
A = a factual claim that is true according to reliable, well-established knowledge.
B = a factual claim that is false according to reliable, well-established knowledge.
C = not a complete, checkable factual claim: a question, opinion, preference, greeting, command, thanks,
     plan or promise, incomplete/cut-off speech, silence, music, or noise.
If a factual statement is ambiguous or you cannot judge its truth reliably, return C.
If speech is still in progress at the end of the audio, return C. Never explain your choice."""


def pcm_to_wav(pcm: bytes, sr: int = 16000) -> bytes:
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return b.getvalue()


def parse(raw: str) -> str:
    """Reject anything other than one allowed label; never infer from an explanation."""
    original = raw.strip()
    label = original.upper()
    if label not in {"A", "B", "C"}:
        # JSON mode is a provider-side constraint; tolerate only the exact one-field form.
        try:
            obj = json.loads(original)
        except (json.JSONDecodeError, TypeError):
            raise ValueError("model did not return exactly A, B, or C")
        if not isinstance(obj, dict) or set(obj) != {"label"} or obj["label"] not in {"A", "B", "C"}:
            raise ValueError("model did not return exactly one A/B/C label")
        label = obj["label"]
    return label


async def classify(client: httpx.AsyncClient, pcm: bytes, model: str,
                   thinking: str = "minimal", timeout: float = 8.0):
    api_key = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPEN_ROUTER_API_KEY", "")
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": "Classify the latest complete statement in this audio. Return only A, B, or C."},
                {"type": "input_audio", "input_audio": {
                    "data": base64.b64encode(pcm_to_wav(pcm)).decode(), "format": "wav"
                }},
            ]},
        ],
        "temperature": 0,
        "max_tokens": 64,
        "reasoning": {"effort": thinking},
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "verdict",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {"label": {"type": "string", "enum": ["A", "B", "C"]}},
                    "required": ["label"],
                    "additionalProperties": False,
                },
            },
        },
    }
    t0 = time.perf_counter()
    r = await client.post(URL, json=body, timeout=timeout, headers={
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": "https://benchmarkheaven.com/audio-jev-bench",
        "X-Title": "Live Fact Check",
    })
    dt = (time.perf_counter() - t0) * 1000
    if r.status_code != 200:
        raise RuntimeError(f"http_{r.status_code}: {r.text[:200]}")
    js = r.json()
    choices = js.get("choices") or []
    raw = ((choices[0].get("message") or {}).get("content") or "") if choices else ""
    label = parse(raw)
    usage = js.get("usage") or {}
    return {"label": label, "latency_ms": round(dt), "usage": {
        "in": usage.get("prompt_tokens"),
        "out": usage.get("completion_tokens"),
    }}
