FROM pixav-photos-canary:maestro-2.10.0
RUN pip install --no-cache-dir pydantic==2.12.5
RUN apt-get update && apt-get install -y --no-install-recommends vlc-bin vlc-plugin-base \
    && rm -rf /var/lib/apt/lists/*
ENV PYTHONPATH=/app/src
WORKDIR /work
