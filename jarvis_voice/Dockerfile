ARG BUILD_FROM
FROM ${BUILD_FROM}
RUN apk add --no-cache python3 py3-pip git ffmpeg alsa-utils alsa-plugins-pulse v4l-utils pulseaudio-utils build-base python3-dev linux-headers
COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt && \
    git init /tmp/cloudapi && cd /tmp/cloudapi && \
    git fetch --depth 1 https://github.com/yandex-cloud/cloudapi.git 6d6e6059ef4885711145410865e0a522eba6127c && \
    git checkout --detach FETCH_HEAD && \
    site_packages="$(/opt/venv/bin/python -c 'import sysconfig; print(sysconfig.get_path("purelib"))')" && \
    /opt/venv/bin/python -m grpc_tools.protoc -I . -I third_party/googleapis \
      --python_out="$site_packages" --grpc_python_out="$site_packages" \
      google/api/http.proto google/api/annotations.proto \
      yandex/cloud/api/operation.proto google/rpc/status.proto \
      yandex/cloud/operation/operation.proto yandex/cloud/validation.proto \
      yandex/cloud/ai/stt/v3/stt_service.proto yandex/cloud/ai/stt/v3/stt.proto \
      yandex/cloud/ai/tts/v3/tts_service.proto yandex/cloud/ai/tts/v3/tts.proto && \
    rm -rf /tmp/cloudapi
COPY *.py /app/
COPY integration/jarvis_q1/ /opt/jarvis_q1_integration/jarvis_q1/
COPY run.sh /run.sh
RUN chmod +x /run.sh && cd /app && /opt/venv/bin/python -E -c 'import main'
CMD ["/run.sh"]
