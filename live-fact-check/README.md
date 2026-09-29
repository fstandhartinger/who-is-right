# Live Fact Check

A small audio demo: send a rolling three-second window from the microphone (or one of the bundled spoken samples) to Gemini 3.5 Flash Lite every 200 ms. The model returns exactly one label: **A** (true), **B** (false), or **C** (not a claim). The page shows each result, recent label agreement, and request latency.

## Run locally

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export GOOGLE_API_KEY='your key in a local shell only'
uvicorn app:app --host 127.0.0.1 --port 8080
```

The app reads the key on the server. It never sends it to the browser or logs it. Audio is kept in the active connection buffer and is not written to disk. Audio requests go to Google's Gemini API.

## Public demo limits

- 200 ms request cadence, only when voice is present, with up to 12 concurrent model calls per session.
- 60 seconds per session, three sessions per client IP per UTC day, four concurrent sessions, and 2,000 model calls per UTC day.
- The global counters live in process memory and reset when the container restarts.

The request cap limits Gemini usage to about $0.44/day at the AudioJevBench v0.1 measured average of $0.22 per 1,000 decisions; actual billing can vary with audio and response tokens. A production multi-replica version would need shared persistent rate-limit storage.
