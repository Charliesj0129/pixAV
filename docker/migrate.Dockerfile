FROM python:3.12-slim

# Install uv
RUN pip install --no-cache-dir uv

# Set working directory
WORKDIR /app

# Copy project files and migrations
COPY . .

# Sync dependencies
RUN uv sync

# Run database migrations
CMD ["uv", "run", "python", "scripts/migrate.py"]
