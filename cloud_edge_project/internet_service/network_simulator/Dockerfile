FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --requirement requirements.txt

RUN groupadd --gid 10001 networksim \
    && useradd --uid 10001 --gid networksim --no-create-home networksim

COPY controller ./controller
COPY domain ./domain
COPY plugins ./plugins
COPY scheduler_stub ./scheduler_stub
COPY config ./config
COPY scripts ./scripts

RUN mkdir -p /app/logs \
    && chown -R networksim:networksim /app/logs

USER networksim

EXPOSE 8090

HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=5 \
    CMD ["python", "-c", "import json, urllib.request; data=json.load(urllib.request.urlopen('http://127.0.0.1:8090/health', timeout=2)); assert data['status'] == 'ok'"]

CMD ["python", "-m", "controller.main"]
