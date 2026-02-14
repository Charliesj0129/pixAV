---
name: docker-patterns
description: Docker and Docker Compose patterns for local development, container security, and multi-service orchestration.
---

# Docker Patterns

Best practices for containerized development in `pixAV`.

## Docker Compose

- **Service Separation**: One process per container.
- **Networking**: Use internal rely on service names (e.g., `redis`, `postgres`).
- **Volumes**: Use named volumes for persistence (`pgdata`).
- **Healthchecks**: Define healthchecks for dependent services.

## Development Workflow

- Run infrastructure: `docker compose up -d postgres redis`
- Run full stack: `docker compose up -d`
- Logs: `docker compose logs -f [service]`
- Clean: `docker compose down -v` (Removes volumes!)

## Security

- **User**: Run as non-root user (`appuser`).
- **Secrets**: Inject via environment variables (`env_file`).
- **Base Images**: Pin versions (e.g., `python:3.10-slim-bullseye`).
- **Scanning**: Use `trivy` or `scout` to scan images.

## Redroid Integration

- **Note**: Redroid containers are managed programmatically via Docker SDK, NOT docker-compose.
- **Network**: Ensure Redroid containers are attached to the same bridge network if needed.
- **ADB**: Connect via IP:PORT.

## Debugging

- Shell into container: `docker compose exec [service] /bin/bash`
- Check resource usage: `docker stats`
