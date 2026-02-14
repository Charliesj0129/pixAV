FROM python:3.12-slim

# Install uv
RUN pip install --no-cache-dir uv

# Set working directory
WORKDIR /app

# Copy project files
COPY . .

# Sync dependencies
RUN uv sync

# Run the pixel injector worker
CMD ["uv", "run", "python", "-m", "pixav.pixel_injector.worker"]
