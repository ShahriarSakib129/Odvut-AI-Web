"""Versioned database migrations for the web platform (and future changes).

Usage
-----
    python scripts/migrate.py --status                 # show applied / pending
    python scripts/migrate.py                          # apply pending (uses DATABASE_URL)
    python scripts/migrate.py --url "postgresql://..."  # explicit database
    python scripts/migrate.py --rollback 0002          # run the .down.sql (destructive for that feature)

Order: ``database/schema.sql`` (baseline, idempotent) first, then every file in
``database/migrations`` in version order. Safe to run repeatedly.
"""

from __future__ import annotations

import argparse
import os
import sys

from _bootstrap import bootstrap, print_header, print_result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply or inspect database migrations")
    parser.add_argument("--url", help="override DATABASE_URL for this run")
    parser.add_argument("--status", action="store_true", help="only print the migration status")
    parser.add_argument("--rollback", metavar="VERSION", help="roll back one migration")
    parser.add_argument("--skip-baseline", action="store_true",
                        help="do not re-run database/schema.sql before the migrations")
    args = parser.parse_args(argv)

    if args.url:
        os.environ["DATABASE_URL"] = args.url
    settings, database = bootstrap()
    import database_migrations as migrations

    print_header("INFO GROUP AI BOT - database migrations")
    print(f"database : {settings.redacted_database_url or '(DATABASE_URL not set)'}")
    if not settings.database_url:
        print_result(False, "DATABASE_URL is empty - set it in .env or pass --url")
        return 2

    factory = database._create_connection  # same session settings as the bot
    try:
        if args.status:
            conn = factory()
            try:
                with conn.cursor() as cur:
                    info = migrations.status(cur)
                conn.commit()
            finally:
                conn.close()
            print(f"known    : {info['known']}")
            print(f"applied  : {info['applied']}")
            print(f"pending  : {info['pending'] or 'none'}")
            if info["checksum_mismatch"]:
                print_result(False, f"edited after apply: {info['checksum_mismatch']}")
                return 1
            return 0

        if args.rollback:
            migrations.rollback(factory, args.rollback)
            print_result(True, f"migration {args.rollback} rolled back")
            return 0

        if not args.skip_baseline:
            if not database.apply_schema_file_only():
                print_result(False, "baseline schema.sql failed - see logs")
                return 1
        applied = migrations.apply_pending(factory)
        print_result(True, f"applied: {applied or 'nothing new'}")
        return 0
    except migrations.MigrationError as exc:
        print_result(False, str(exc))
        return 1
    finally:
        database.close_pool()


if __name__ == "__main__":
    sys.exit(main())
