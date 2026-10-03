FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/data/podbridge.db

WORKDIR /app

RUN useradd --system --uid 1000 --home-dir /app podbridge \
    && mkdir /data && chown podbridge /data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY podbridge ./podbridge
COPY wsgi.py .

USER podbridge
VOLUME /data
EXPOSE 7330

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7330/healthz', timeout=4)"

# One worker: the scheduler (Phase 4) runs in-process and must exist exactly once.
CMD ["gunicorn", "--bind", "0.0.0.0:7330", "--workers", "1", "--threads", "4", \
     "--access-logfile", "-", "wsgi:app"]
