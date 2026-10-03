# The cold read-back environment for the managed storage worker.
#
# It exists so the read-back runs somewhere that has a browser and nothing else:
# no staging mount, no guest /data, no database credentials. Reusing the worker
# image would put Chromium next to the bytes it is supposed to fetch from the
# provider, and "cold_inputs: provider-only" would stop being provable.
#
# Build:
#   docker build -f docker/storage-tools.Dockerfile -t pixav-storage-tools:1 .
FROM pixav-photos-canary:maestro-2.10.0
# inspect_media uses metadata.probe_media, whose module also imports httpx.
RUN pip install --no-cache-dir pydantic==2.12.5 httpx==0.28.1
COPY src/ /app/src/
ENV PYTHONPATH=/app/src
# Exercise the lazy import used after an actual browser download at build time.
RUN python -c "from pixav.media_loader.metadata import probe_media; from pixav.pixel_injector.segment_readback import read_back"
WORKDIR /work
