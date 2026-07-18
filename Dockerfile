FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY narratordb ./narratordb
RUN python -m pip install --no-cache-dir '.[mcp]'

VOLUME ["/data"]
EXPOSE 8787
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=2).read()"]

CMD ["narratordb", "service", "quickstart", "--data-dir", "/data", "--credentials-file", "/data/credentials.env", "--project", "default", "--no-register-codex", "--host", "0.0.0.0", "--port", "8787", "--public-url", "http://127.0.0.1:8787"]
