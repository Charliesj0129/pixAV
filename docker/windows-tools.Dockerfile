ARG PIXAV_WINDOWS_TOOLS_BASE_IMAGE
FROM ${PIXAV_WINDOWS_TOOLS_BASE_IMAGE}

# Extend the verified immutable Android tools image with reviewed flows only.
# Accounts, AVDs, DB credentials and application workers stay outside this image.
COPY config/maestro/ /flows/
# Keep media protection in the process that actually reads /parts. These two
# dependency-free modules do not introduce application workers or credentials.
COPY src/pixav/shared/media_file.py /app/src/pixav/shared/media_file.py
COPY src/pixav/pixel_injector/adb_media.py /app/src/pixav/pixel_injector/adb_media.py
ENV PYTHONPATH=/app/src
LABEL pixav.windows-tools.schema="2"
