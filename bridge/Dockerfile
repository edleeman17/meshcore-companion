# Bridge + web app in one container. USB radios only: pass the serial port in with
# `devices:` (see docker-compose.yml). Bluetooth still runs on the host (README).
# Build from the repo root: docker build -f bridge/Dockerfile .
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data PWA_DIR=/app/pwa
WORKDIR /app
COPY bridge/requirements.txt .
RUN pip install -r requirements.txt
# dialout (gid 20) owns /dev/ttyACM* on Debian/Ubuntu/Raspberry Pi OS hosts; other
# hosts set RADIO_GID and docker-compose.yml adds that group at run time
RUN useradd -r -u 1000 -G dialout bridge && mkdir /data && chown bridge /data
COPY bridge/app app
COPY bridge/docker-entrypoint.sh /usr/local/bin/
COPY pwa/static pwa
USER bridge
VOLUME /data
EXPOSE 8190
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8190/health', timeout=4)"
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8190", "--no-access-log"]
