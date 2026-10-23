ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    NE8000_DATA_DIR=/data NE8000_HOST=0.0.0.0 NE8000_PORT=8080

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && useradd --system --uid 1000 --home /app ne8000 \
 && mkdir -p /data && chown ne8000 /data
COPY --chown=ne8000 . .

USER ne8000
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ['NE8000_PORT'])"
CMD ["python", "app.py"]
