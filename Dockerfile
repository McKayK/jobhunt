FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    JOBHUNT_DATA_DIR=/data

WORKDIR /srv

RUN apt-get update && apt-get install -y --no-install-recommends \
        tini curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir --no-deps python-jobspy

COPY app/ ./app/
COPY web/ ./web/

RUN useradd -u 1000 -m jobhunt && mkdir -p /data && chown -R jobhunt /data /srv
USER jobhunt

VOLUME ["/data"]
EXPOSE 8081

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8081/api/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.server:app", "--host", "0.0.0.0", "--port", "8081"]
