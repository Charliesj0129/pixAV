FROM python:3.12-slim

# Install uv
RUN pip install --no-cache-dir uv

# Set working directory
WORKDIR /app

# Copy project files and migrations
COPY . .

# Sync runtime dependencies only (exclude local dev/test groups)
RUN uv sync --frozen --no-group dev --no-group embeddings

# Run database migrations
CMD ["uv", "run", "--no-sync", "python", "scripts/migrate.py"]
