"""Versioned, checksum-verified database migrations.

Layout
------
``database/schema.sql``                 idempotent baseline (existing, unchanged)
``database/migrations/NNNN_name.sql``    forward migrations, applied in order
``database/migrations/NNNN_name.down.sql`` optional rollback, run manually only

Guarantees
----------
* each migration runs inside one transaction -> all or nothing
* applied migrations are recorded in ``public.schema_migrations`` with a SHA-256
  checksum; a file edited after it was applied is reported as a hard error
  instead of being silently re-run
* the runner is safe to call repeatedly (idempotent)
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from config import PROJECT_ROOT

MIGRATIONS_DIR = PROJECT_ROOT / "database" / "migrations"

_CREATE_TABLE = """
create table if not exists public.schema_migrations (
    version     text primary key,
    name        text not null,
    checksum    text not null,
    applied_at  timestamptz not null default now()
)
"""


class MigrationError(RuntimeError):
    """Raised when a migration cannot be applied safely."""


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    path: Path
    checksum: str
    rollback_path: Path | None

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Return forward migrations sorted by version."""
    if not directory.exists():
        return []
    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        if path.name.endswith(".down.sql"):
            continue
        stem = path.stem                       # e.g. 0002_web_platform
        version, _, name = stem.partition("_")
        if not version.isdigit():
            raise MigrationError(f"migration file must start with NNNN_: {path.name}")
        text = path.read_bytes()
        rollback = path.with_name(f"{stem}.down.sql")
        migrations.append(Migration(
            version=version.zfill(4),
            name=name or stem,
            path=path,
            checksum=hashlib.sha256(text).hexdigest(),
            rollback_path=rollback if rollback.exists() else None,
        ))
    versions = [m.version for m in migrations]
    if len(versions) != len(set(versions)):
        raise MigrationError("duplicate migration version numbers detected")
    return migrations


def _ensure_table(cur: Any) -> None:
    cur.execute(_CREATE_TABLE)


def status(cursor: Any, migrations: Iterable[Migration] | None = None) -> dict[str, Any]:
    """Compare files with the recorded state. Read-only."""
    migrations = list(migrations if migrations is not None else discover())
    cursor.execute(
        "select to_regclass('public.schema_migrations') is not null"
    )
    has_table = bool(cursor.fetchone()[0])
    applied: dict[str, tuple[str, str]] = {}
    if has_table:
        cursor.execute("select version, checksum, name from public.schema_migrations")
        applied = {row[0]: (row[1], row[2]) for row in cursor.fetchall()}
    pending = [m.version for m in migrations if m.version not in applied]
    tampered = [m.version for m in migrations
                if m.version in applied and applied[m.version][0] != m.checksum]
    return {
        "table_present": has_table,
        "applied": sorted(applied),
        "pending": pending,
        "checksum_mismatch": tampered,
        "known": [m.version for m in migrations],
    }


def apply_pending(connection_factory: Callable[[], Any], *,
                  migrations: Iterable[Migration] | None = None,
                  log: Callable[[str], None] = print) -> list[str]:
    """Apply every pending migration. Returns the list of versions applied.

    ``connection_factory`` must return a psycopg2-compatible connection with
    autocommit disabled. The function commits per migration.
    """
    migrations = list(migrations if migrations is not None else discover())
    applied_now: list[str] = []
    conn = connection_factory()
    try:
        with conn.cursor() as cur:
            _ensure_table(cur)
        conn.commit()

        with conn.cursor() as cur:
            cur.execute("select version, checksum from public.schema_migrations")
            recorded = {row[0]: row[1] for row in cur.fetchall()}
        for migration in migrations:
            if migration.version in recorded:
                if recorded[migration.version] != migration.checksum:
                    raise MigrationError(
                        f"migration {migration.version} ({migration.name}) changed after it "
                        "was applied; create a new migration instead of editing this one"
                    )
                continue
            log(f"applying migration {migration.version} {migration.name}")
            try:
                with conn.cursor() as cur:
                    cur.execute(migration.sql)
                    cur.execute(
                        "insert into public.schema_migrations (version, name, checksum) "
                        "values (%s, %s, %s)",
                        (migration.version, migration.name, migration.checksum),
                    )
                conn.commit()
            except Exception as exc:
                conn.rollback()
                raise MigrationError(
                    f"migration {migration.version} failed and was rolled back: "
                    f"{str(exc).splitlines()[0][:300]}"
                ) from exc
            applied_now.append(migration.version)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return applied_now


def rollback(connection_factory: Callable[[], Any], version: str, *,
             migrations: Iterable[Migration] | None = None,
             log: Callable[[str], None] = print) -> bool:
    """Run the ``.down.sql`` of one applied migration and forget its record."""
    migrations = list(migrations if migrations is not None else discover())
    target = next((m for m in migrations if m.version == version.zfill(4)), None)
    if target is None:
        raise MigrationError(f"unknown migration version {version}")
    if target.rollback_path is None:
        raise MigrationError(f"migration {version} has no rollback script")
    down_sql = target.rollback_path.read_text(encoding="utf-8")
    conn = connection_factory()
    try:
        log(f"rolling back migration {target.version} {target.name}")
        with conn.cursor() as cur:
            cur.execute(down_sql)
            cur.execute("delete from public.schema_migrations where version = %s",
                        (target.version,))
        conn.commit()
        return True
    except Exception as exc:
        conn.rollback()
        raise MigrationError(f"rollback failed: {str(exc).splitlines()[0][:300]}") from exc
    finally:
        try:
            conn.close()
        except Exception:
            pass
