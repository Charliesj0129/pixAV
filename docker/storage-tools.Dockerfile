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
RUN pip install --no-cache-dir pydantic==2.12.5
ENV PYTHONPATH=/app/src
WORKDIR /work
