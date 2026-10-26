FROM python:3.13.9-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ERL_SKILL_ROOT=/opt/evidence-research-loop \
    ERL_PROJECTS_ROOT=/data/projects \
    ERL_RUN_UID=20000 \
    ERL_RUN_GID=20000

RUN groupadd --gid 20000 erlrunner \
    && useradd --uid 20000 --gid 20000 --home-dir /nonexistent --shell /usr/sbin/nologin erlrunner \
    && mkdir -p /app /opt/evidence-research-loop /data/projects \
    && chown -R 20000:20000 /data

WORKDIR /app
COPY requirements.txt requirements.lock /app/
RUN pip install --no-cache-dir --require-hashes -r /app/requirements.lock

COPY server.py worker.py run_local.py version.json /app/
COPY evidence-research-loop/ /opt/evidence-research-loop/
RUN chmod -R a=rX /app /opt/evidence-research-loop \
    && chmod 0755 /app/server.py /app/worker.py

USER 20000:20000

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).read()"

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]

