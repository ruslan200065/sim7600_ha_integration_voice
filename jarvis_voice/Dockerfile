ARG BUILD_FROM

# ---------------------------------------------------------------------------
# Builder: compilers, git, grpcio-tools. Produces /opt/venv with the runtime
# dependencies and the generated Yandex cloudapi stubs in its site-packages.
# ---------------------------------------------------------------------------
FROM ${BUILD_FROM} AS builder
RUN apk add --no-cache python3 py3-pip git build-base python3-dev linux-headers
COPY requirements.txt requirements-build.txt /tmp/
RUN python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt -r /tmp/requirements-build.txt && \
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
    cd / && rm -rf /tmp/cloudapi && \
    /opt/venv/bin/pip uninstall -y grpcio-tools && \
    /opt/venv/bin/pip check

# ---------------------------------------------------------------------------
# Runtime: same Home Assistant base, runtime packages only.
# ---------------------------------------------------------------------------
FROM ${BUILD_FROM}
# libstdc++/libgcc: grpcio's native extension links against them (they were
# previously pulled in implicitly by build-base).
RUN apk add --no-cache python3 tzdata ffmpeg alsa-utils alsa-plugins-pulse v4l-utils pulseaudio-utils libstdc++ libgcc
COPY --from=builder /opt/venv /opt/venv
COPY *.py /app/
COPY web/ /app/web/
COPY integration/jarvis_q1/ /opt/jarvis_q1_integration/jarvis_q1/
COPY run.sh /run.sh
RUN chmod +x /run.sh && cd /app && /opt/venv/bin/python -E -c 'import main' && \
    ! /opt/venv/bin/python -E -c 'import grpc_tools' 2>/dev/null
CMD ["/run.sh"]
