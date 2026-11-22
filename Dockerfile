# Pinned to a release branch so rebuilds are reproducible; bump deliberately.
ARG ALPINE_VERSION=3.24
FROM alpine:${ALPINE_VERSION}

# We pass this variable from GitHub Actions. Default is 'cpu'.
# Values: cpu, intel, amd, latest (= intel + amd drivers)
ARG HW_TYPE=cpu

# Base dependencies plus hardware-specific drivers in a single layer.
# (--no-cache already fetches a fresh index, so no separate `apk update`.)
RUN set -eux; \
    case "$HW_TYPE" in \
        cpu)    HW_PKGS="" ;; \
        intel)  HW_PKGS="intel-media-driver libva-intel-driver libva-utils" ;; \
        amd)    HW_PKGS="mesa-va-gallium libva-utils" ;; \
        latest) HW_PKGS="intel-media-driver libva-intel-driver mesa-va-gallium libva-utils" ;; \
        *)      echo "Unknown HW_TYPE '$HW_TYPE' (use cpu, intel, amd or latest)" >&2; exit 1 ;; \
    esac; \
    apk add --no-cache \
        python3 \
        ffmpeg \
        libavif-apps \
        libheif-tools \
        exiftool \
        $HW_PKGS; \
    ln -sf /usr/bin/python3 /usr/bin/python; \
    mkdir -p /data/input /data/output /app

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Scripts last, so code changes don't invalidate the package layer.
COPY --chmod=755 entrypoint.sh /app/entrypoint.sh
COPY --chmod=644 common.py converter.py check.py /app/

CMD ["/app/entrypoint.sh"]
