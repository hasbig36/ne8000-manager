# Build from the repository root:  docker compose -f store/docker-compose.yml up -d --build
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 STORE_DATA_DIR=/data STORE_HOST=0.0.0.0 STORE_PORT=8090 \
    STORE_APP_DIR=/srv/ne8000 STORE_BEHIND_PROXY=1
COPY store/requirements.txt /tmp/req.txt
RUN pip install --no-cache-dir -r /tmp/req.txt && useradd --system --uid 1000 store && mkdir -p /data && chown store /data
# the NE8000 Manager app that buyers download (the store itself is left out of their download)
COPY --chown=store . /srv/ne8000
WORKDIR /srv/ne8000/store
USER store
VOLUME ["/data"]
EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8090/healthz')"
CMD ["python", "app.py"]
