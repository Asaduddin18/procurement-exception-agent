"""Apply schema migrations and load reference fixtures into a local database."""

import argparse
import os
from dataclasses import astuple
from hashlib import sha256
from pathlib import Path

import psycopg

from seed.fixtures import (
    CATALOG,
    CHUNKS,
    EMBEDDING_DIMENSIONS,
    FIXTURE_EMBEDDING_SPACE,
    POLICIES,
    RULES,
    VENDORS,
)

DEFAULT_DATABASE_URL = "postgresql://procurement:procurement@127.0.0.1:5432/procurement"
MIGRATIONS = Path(__file__).with_name("migrations")
BOOTSTRAP_LOCK = 767319026


def migrate(connection: psycopg.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
            migration_name text PRIMARY KEY,
            checksum text NOT NULL,
            applied_at timestamptz NOT NULL DEFAULT now()
        )"""
    )
    applied = dict(connection.execute("SELECT migration_name, checksum FROM schema_migrations"))
    for path in sorted(MIGRATIONS.glob("[0-9]*.sql")):
        migration = path.read_bytes()
        checksum = sha256(migration).hexdigest()
        if path.name in applied:
            if applied[path.name] != checksum:
                raise RuntimeError(f"Applied migration has changed: {path.name}")
            continue
        connection.execute(migration.decode("utf-8"))
        connection.execute(
            "INSERT INTO schema_migrations (migration_name, checksum) VALUES (%s, %s)",
            (path.name, checksum),
        )


def seed_reference_data(cursor: psycopg.Cursor) -> None:
    cursor.executemany(
        """INSERT INTO vendors (vendor_id, vendor_name, payment_terms_days, contract_reference)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT (vendor_id) DO NOTHING""",
        VENDORS,
    )
    cursor.executemany(
        """INSERT INTO price_catalog (vendor_id, sku, description, currency, unit, unit_price)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (vendor_id, sku) DO NOTHING""",
        CATALOG,
    )
    cursor.executemany(
        """INSERT INTO sop_policy_versions
              (policy_id, version, status, effective_date, category, region, title)
           VALUES (%s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (policy_id, version) DO NOTHING""",
        [astuple(policy) for policy in POLICIES],
    )
    cursor.executemany(
        """INSERT INTO sop_policy_rules (policy_id, version, rule_code, rule_value, currency)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (policy_id, version, rule_code) DO NOTHING""",
        RULES,
    )
    chunk_records = []
    for *fields, weights in CHUNKS:
        padded_weights = [*weights, *([0] * (EMBEDDING_DIMENSIONS - len(weights)))]
        embedding = "[" + ",".join(map(str, padded_weights)) + "]"
        chunk_records.append((*fields, FIXTURE_EMBEDDING_SPACE, embedding))
    cursor.executemany(
        """INSERT INTO sop_policy_chunks
              (policy_id, version, chunk_number, source_location, content, embedding_space, embedding)
           VALUES (%s, %s, %s, %s, %s, %s, %s::vector)
           ON CONFLICT (policy_id, version, chunk_number) DO NOTHING""",
        chunk_records,
    )


def bootstrap(database_url: str, *, migrate_only: bool = False) -> None:
    with psycopg.connect(database_url) as connection:
        with connection.transaction():
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (BOOTSTRAP_LOCK,))
            migrate(connection)
            if not migrate_only:
                with connection.cursor() as cursor:
                    seed_reference_data(cursor)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migrate-only", action="store_true", help="Apply schema without demo data.")
    args = parser.parse_args()
    bootstrap(os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL), migrate_only=args.migrate_only)
    print("Schema is current." if args.migrate_only else "Schema and procurement fixtures are ready.")


if __name__ == "__main__":
    main()
