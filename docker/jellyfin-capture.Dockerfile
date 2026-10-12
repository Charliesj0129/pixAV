# Builds an isolated headed browser and real X11/PulseAudio capture environment.
# Pass a previously inspected tools image reference; pin the resulting image ID.
ARG CAPTURE_BASE_IMAGE
FROM ${CAPTURE_BASE_IMAGE}
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb xauth openbox xdotool pulseaudio pulseaudio-utils \
    && rm -rf /var/lib/apt/lists/*
ARG CAPTURE_BROWSER_CHANNEL=chromium
RUN case "$CAPTURE_BROWSER_CHANNEL" in \
      chromium) ;; \
      chrome) python -m playwright install chrome ;; \
      *) exit 2 ;; \
    esac
COPY src/ /app/src/
COPY scripts/capture_jellyfin_av.py /app/capture_jellyfin_av.py
ENV PYTHONPATH=/app/src
RUN chmod -R a+rX /app/src && chmod a+r /app/capture_jellyfin_av.py && \
    python -m py_compile /app/capture_jellyfin_av.py && \
    python -c "from pixav.strm_resolver.av_acceptance import SyntheticAVSpec, analyze_window"
ENV HOME=/home/ubuntu
USER ubuntu
RUN python /app/capture_jellyfin_av.py --help >/dev/null
WORKDIR /work
ENTRYPOINT ["python", "/app/capture_jellyfin_av.py"]
