"""Gemini audio call: send a three-second WAV and accept exactly one A/B/C label."""
import base64
import io
import json
import os
import time
import wave

import httpx

URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

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
    body = {
        "systemInstruction": {"parts": [{"text": PROMPT}]},
        "contents": [{"role": "user", "parts": [
            {"text": "Classify the latest complete statement in this audio. Return only A, B, or C."},
            {"inlineData": {"mimeType": "audio/wav", "data": base64.b64encode(pcm_to_wav(pcm)).decode()}},
        ]}],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": 64,
            "responseMimeType": "application/json",
            "responseSchema": {"type": "OBJECT", "properties": {"label": {"type": "STRING", "enum": ["A", "B", "C"]}}, "required": ["label"], "propertyOrdering": ["label"]},
            "thinkingConfig": {"thinkingLevel": thinking},
        },
    }
    t0 = time.perf_counter()
    r = await client.post(URL.format(model=model), json=body, timeout=timeout,
                          headers={"x-goog-api-key": os.environ.get("GOOGLE_API_KEY", "")})
    dt = (time.perf_counter() - t0) * 1000
    if r.status_code != 200:
        raise RuntimeError(f"http_{r.status_code}: {r.text[:200]}")
    js = r.json()
    parts = ((js.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
    raw = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    label = parse(raw)
    usage = js.get("usageMetadata") or {}
    return {"label": label, "latency_ms": round(dt), "usage": {
        "in": usage.get("promptTokenCount"),
        "out": (usage.get("candidatesTokenCount") or 0) + (usage.get("thoughtsTokenCount") or 0),
    }}
