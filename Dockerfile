FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py ./
COPY public ./public
# Structured JSON logs + request ids (logging standard, AGENTS.md; logkit/README.md). No IPs or user agents.
COPY logkit/logkit.py logkit/logkit_asgi.py ./
ENV LOG_APP=who-is-right LOG_IP=0 LOG_UA=0 LOG_ASGI_TARGET=app:app
EXPOSE 8080
CMD ["uvicorn","logkit_asgi:app","--host","0.0.0.0","--port","8080","--proxy-headers","--forwarded-allow-ips=*"]
