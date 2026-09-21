# Who Is Right?!

A comic, live argument fact-check party demo. Microphone audio travels over an
HTTPS WebSocket to the backend, which opens a Gemini Live session. Gemini
transcribes, identifies checkable claims, and calls a backend tool. One Jev
`classifier-fast` decision per claim then moves the gauge.

This is entertainment, not a factual authority. Audio is processed live only;
the application does not record or store it.

## Run

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
GOOGLE_API_KEY=... uvicorn app:app --port 8080
```

Production defaults: 180 seconds per session, three sessions per IP per UTC
day, 100 global sessions and 300 Jev calls per UTC day, five concurrent
sessions. Override the corresponding environment variables to lower them.

MIT licensed.

