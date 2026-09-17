#!/usr/bin/env python3
"""Create the dedicated login roles the managed workflow refuses to run without.

``require_workflow_role`` rejects a superuser and rejects a login that is a
member of both group roles, because the whole point of the split is that an
activity worker cannot advance an execution and the execution authority never
touches a provider. Migration 012 creates the two NOLOGIN group roles; the
logins that inherit them are deployment identities, not schema, so they live
here instead of in a migration.

Passwords are read from the environment and are never printed, logged, or
written to a file:

    PIXAV_AUTHORITY_ROLE_PASSWORD='...' \
    PIXAV_WORKER_ROLE_PASSWORD='...' \
    uv run python scripts/provision_workflow_roles.py --apply

The default is a dry run: it reports what would change and writes nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
from typing import Any

from pixav.config import get_settings
from pixav.shared.db import create_pool
from pixav.shared.instance import database_identity

# login role -> the NOLOGIN group role that carries its privileges
ROLE_PLAN: tuple[tuple[str, str, str], ...] = (
    ("pixav_authority", "pixav_execution_authority", "PIXAV_AUTHORITY_ROLE_PASSWORD"),
    ("pixav_worker", "pixav_activity_worker", "PIXAV_WORKER_ROLE_PASSWORD"),
)

_SAFE_ROLE = re.compile(r"^[a-z][a-z0-9_]{2,62}$")


def _password(variable: str) -> str:
    value = os.environ.get(variable, "")
    if not value.strip():
        raise SystemExit(f"{variable} is required (never hardcode credentials in this script)")
    return value


def _quote(identifier: str) -> str:
    """Refuse anything that is not a plain lowercase SQL identifier."""
    if not _SAFE_ROLE.fullmatch(identifier):
        raise ValueError(f"unsupported role name: {identifier!r}")
    return f'"{identifier}"'


def _literal(value: str) -> str:
    """Return a single-quoted SQL string literal for a password."""
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


async def _observe(pool: Any) -> dict[str, dict]:
    rows = await pool.fetch(
        """SELECT r.rolname, r.rolcanlogin, r.rolsuper,
        ARRAY(SELECT g.rolname FROM pg_auth_members m JOIN pg_roles g ON g.oid=m.roleid
              WHERE m.member=r.oid ORDER BY 1) AS memberships
        FROM pg_roles r WHERE r.rolname = ANY($1::text[])""",
        [name for name, _, _ in ROLE_PLAN] + [group for _, group, _ in ROLE_PLAN],
    )
    return {row["rolname"]: dict(row) for row in rows}


async def _provision(pool: Any, *, name: str, group: str, password: str, exists: bool) -> None:
    """Create or align one login role and its single group membership."""
    async with pool.acquire() as connection, connection.transaction():
        verb = "ALTER" if exists else "CREATE"
        await connection.execute(f"{verb} ROLE {_quote(name)} LOGIN INHERIT NOSUPERUSER")
        # DDL cannot take a bind parameter, so the password becomes a quoted
        # literal. It is never logged or printed: only the role name is.
        await connection.execute(f"ALTER ROLE {_quote(name)} PASSWORD {_literal(password)}")
        await connection.execute(f"GRANT {_quote(group)} TO {_quote(name)}")
        # The split only holds if neither login can borrow the other's group;
        # revoking is a no-op when the membership was never there.
        for _, other, _unused in ROLE_PLAN:
            if other != group:
                await connection.execute(f"REVOKE {_quote(other)} FROM {_quote(name)}")
        await connection.execute(f"GRANT USAGE ON SCHEMA public TO {_quote(name)}")


def _report(observed: dict[str, dict], *, applied: bool) -> None:
    """Print what the cluster now holds, so the operator checks the result."""
    for name, _group, _variable in ROLE_PLAN:
        row = observed.get(name)
        if row is None:
            print(f"{name}: still absent (dry run)" if not applied else f"{name}: MISSING after apply")
            continue
        print(
            f"{name}: login={row['rolcanlogin']} superuser={row['rolsuper']} "
            f"memberships={sorted(row['memberships'])}"
        )
    if not applied:
        print("dry run: nothing was written; re-run with --apply")


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    pool = await create_pool(settings)
    try:
        identity = await database_identity(pool)
        print(f"database identity: {identity}")
        print(f"database name: {settings.db_name}")
        if args.db_identity and args.db_identity != identity:
            print(f"refusing: expected database identity {args.db_identity}")
            return 2

        observed = await _observe(pool)
        missing_groups = [group for _, group, _ in ROLE_PLAN if group not in observed]
        if missing_groups:
            print(f"refusing: apply migration 012 first; missing group role(s) {', '.join(missing_groups)}")
            return 2

        # Read every password before writing anything, so a missing variable
        # cannot leave one role provisioned and the other absent.
        secrets = {name: _password(variable) for name, _, variable in ROLE_PLAN} if args.apply else {}

        for name, group, _ in ROLE_PLAN:
            existing = observed.get(name)
            state = "absent" if existing is None else "present"
            print(f"{name}: {state}, target = LOGIN + member of {group}, NOSUPERUSER")
            if args.apply:
                await _provision(pool, name=name, group=group, password=secrets[name], exists=existing is not None)
                print(f"{name}: provisioned")

        _report(await _observe(pool), applied=args.apply)
        return 0
    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-identity", default=None, help="refuse to act on a different PostgreSQL cluster")
    parser.add_argument("--apply", action="store_true", help="write the roles (default: dry run)")
    raise SystemExit(asyncio.run(_run(parser.parse_args())))


if __name__ == "__main__":
    main()
