"""logkit ASGI wrapper — structured logs for any ASGI app (FastAPI/Starlette) WITHOUT touching its code.
    LOG_ASGI_TARGET=app.main:app uvicorn logkit_asgi:app --no-access-log ...
Env: LOG_APP, LOG_LEVEL, LOG_IP=0 / LOG_UA=0 (leave out hashed IP / user agent if the privacy page promises it).
Sets up JSON logging (uvicorn + app loggers), then wraps the target app with logkit.ASGIMiddleware (outermost):
request id in/out (X-Request-Id), one http.request line per request (route resolved after routing), errors with stack.
"""
import importlib
import os

import logkit

log = logkit.setup()
_mod, _, _attr = os.environ["LOG_ASGI_TARGET"].partition(":")
target = getattr(importlib.import_module(_mod), _attr or "app")
app = logkit.ASGIMiddleware(target, logger=log, ip=os.environ.get("LOG_IP") != "0", ua=os.environ.get("LOG_UA") != "0")
