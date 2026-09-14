#!/usr/bin/env python3
"""Operator entry point for the migration runner.

The ordering rules and the applier live in ``pixav.shared.migrations``; this
file stays because ``docker/migrate.Dockerfile`` runs it by path, and because
the single-film CLI imports ``run_migrations`` from here while a run is in
flight — a supervisor re-executes that CLI from disk between segments.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os

from pixav.config import get_settings
from pixav.shared.migrations import MIGRATIONS_DIR, run_migrations, select_pending

__all__ = ["MIGRATIONS_DIR", "main", "run_migrations", "select_pending"]

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def main() -> None:
    """Apply migrations against PIXAV_DSN, or the configured database.

    The fallback goes through ``Settings`` rather than a literal DSN so the
    runner picks up PIXAV_DB_HOST/PORT/USER/PASSWORD/NAME like every other
    component — a hardcoded ``localhost`` default resolves to the container
    itself when this runs as the compose ``migrate`` service.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--until",
        default=None,
        metavar="FILENAME",
        help="apply migrations up to and including this file, then stop",
    )
    args = parser.parse_args()

    dsn = os.environ.get("PIXAV_DSN", "").strip() or get_settings().dsn
    try:
        asyncio.run(run_migrations(dsn, until=args.until))
    except ValueError as exc:
        parser.exit(2, f"migration refused: {exc}\n")


if __name__ == "__main__":
    main()
