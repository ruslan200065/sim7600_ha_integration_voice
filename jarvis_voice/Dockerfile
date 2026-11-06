ARG BUILD_FROM
FROM ${BUILD_FROM}
RUN apk add --no-cache python3 py3-pip git ffmpeg alsa-utils alsa-plugins-pulse v4l-utils pulseaudio-utils build-base python3-dev linux-headers
COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt && \
    git clone --depth 1 https://github.com/yandex-cloud/cloudapi.git /tmp/cloudapi && \
    mkdir -p /opt/protos && cd /tmp/cloudapi && \
    /opt/venv/bin/python -m grpc_tools.protoc -I . -I third_party/googleapis \
      --python_out=/opt/protos --grpc_python_out=/opt/protos \
      google/api/http.proto google/api/annotations.proto \
      yandex/cloud/api/operation.proto google/rpc/status.proto \
      yandex/cloud/operation/operation.proto yandex/cloud/validation.proto \
      yandex/cloud/ai/stt/v3/stt_service.proto yandex/cloud/ai/stt/v3/stt.proto \
      yandex/cloud/ai/tts/v3/tts_service.proto yandex/cloud/ai/tts/v3/tts.proto && \
    rm -rf /tmp/cloudapi
COPY *.py /app/
COPY run.sh /run.sh
RUN chmod +x /run.sh
ENV PYTHONPATH=/opt/protos:/app PYTHONUNBUFFERED=1
CMD ["/run.sh"]
