ARG PIXAV_WINDOWS_TOOLS_BASE_IMAGE
FROM ${PIXAV_WINDOWS_TOOLS_BASE_IMAGE}

# Extend the verified immutable Android tools image with reviewed flows only.
# Accounts, AVDs, DB credentials and application workers stay outside this image.
COPY config/maestro/ /flows/
LABEL pixav.windows-tools.schema="1"
