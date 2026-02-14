FROM python:3.12-slim

# Install uv
RUN pip install --no-cache-dir uv

# Set working directory
WORKDIR /app

# Copy project files
COPY . .

# Sync dependencies
RUN uv sync

# Run the STRM resolver API
CMD ["uv", "run", "uvicorn", "pixav.strm_resolver.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
