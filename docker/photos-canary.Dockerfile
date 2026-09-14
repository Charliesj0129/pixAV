FROM mcr.microsoft.com/playwright/python:v1.55.0-noble

# Isolated tools only: no application worker, database or queue configuration.
ARG MAESTRO_VERSION=2.10.0
ARG MAESTRO_SHA256=29b675e10cc12080e445e9bfb2e2b4e4dfb9c0f2e30d5884120d258b5e1cd991
RUN apt-get update && apt-get install -y --no-install-recommends \
    adb ffmpeg openjdk-21-jre-headless curl unzip \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL "https://github.com/mobile-dev-inc/maestro/releases/download/cli-${MAESTRO_VERSION}/maestro.zip" -o /tmp/maestro.zip \
    && echo "${MAESTRO_SHA256}  /tmp/maestro.zip" | sha256sum -c - \
    && unzip -q /tmp/maestro.zip -d /opt && rm /tmp/maestro.zip \
    && pip install --no-cache-dir playwright==1.55.0
ENV PATH="/opt/maestro/bin:${PATH}" \
    MAESTRO_CLI_NO_ANALYTICS=1
WORKDIR /work
CMD ["sleep", "infinity"]
