ARG NODE_IMAGE=node:22-bookworm-slim
ARG PYTHON_IMAGE=python:3.11-slim-trixie
FROM ${NODE_IMAGE} AS node
FROM ${PYTHON_IMAGE}

LABEL org.opencontainers.image.title="DouyinLiveRecorder-Web" \
      org.opencontainers.image.source="https://github.com/MalcolmLu/DouyinLiveRecorder-Web" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="1.1.1"

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Asia/Shanghai
WORKDIR /app

# Debian's full Intel media driver includes the QSV encode support.
RUN sed -i 's/^Components: main$/Components: main non-free non-free-firmware/' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates ffmpeg openssl tzdata libatomic1 \
       intel-media-va-driver-non-free libmfx-gen1.2 libvpl2 vainfo \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY requirements.txt /app/requirements.txt
COPY gui/requirements.txt gui/requirements-youtube.txt /app/gui/
RUN pip install --no-cache-dir -r requirements.txt -r gui/requirements.txt -r gui/requirements-youtube.txt
COPY . /app
RUN chmod +x /app/scripts/entrypoint.sh \
    && node --version \
    && ffmpeg -hide_banner -encoders 2>/dev/null | grep -q hevc_qsv

EXPOSE 18888
ENTRYPOINT ["/app/scripts/entrypoint.sh"]
CMD ["python", "-u", "/app/main.py"]
