from datetime import date
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest

from seed import seed


def test_seed_is_repeatable_and_keeps_policy_scope(database_url):
    seed.bootstrap(database_url)
    with psycopg.connect(database_url) as connection:
        query = """
            SELECT (SELECT count(*) FROM schema_migrations),
                   (SELECT count(*) FROM vendors),
                   (SELECT count(*) FROM price_catalog),
                   (SELECT count(*) FROM sop_policy_versions),
                   (SELECT count(*) FROM sop_policy_rules),
                   (SELECT count(*) FROM sop_policy_chunks)
        """
        assert connection.execute(query).fetchone() == (8, 3, 6, 4, 6, 6)
        seed.bootstrap(database_url)
        assert connection.execute(query).fetchone() == (8, 3, 6, 4, 6, 6)
        assert connection.execute("""
            SELECT payment_terms_days, unit_price
            FROM vendors JOIN price_catalog USING (vendor_id)
            WHERE vendor_name = 'Apex Industrial Supply' AND sku = 'SS-316 Valve'
        """).fetchone() == (30, Decimal("120.0000"))
        assert connection.execute("""
            SELECT status, effective_date, category, region, rule_value, currency
            FROM sop_policy_versions JOIN sop_policy_rules USING (policy_id, version)
            WHERE rule_code = 'FINANCE_DIRECTOR_CAP'
            ORDER BY version, region
        """).fetchall() == [
            ("ARCHIVED", date(2024, 1, 1), "procurement", "US", Decimal("5000"), "USD"),
            ("ACTIVE", date(2026, 1, 1), "freight", "EU", Decimal("7500"), "EUR"),
            ("ACTIVE", date(2026, 1, 1), "procurement", "US", Decimal("10000"), "USD"),
            ("ACTIVE", date(2027, 1, 1), "procurement", "US", Decimal("12500"), "USD"),
        ]
        assert connection.execute("""
            SELECT DISTINCT vector_dims(embedding), embedding_space FROM sop_policy_chunks
        """).fetchall() == [(1536, "procurement-fixture-v1")]


def test_seed_rolls_back_on_invalid_chunk(database_url, monkeypatch):
    seed.bootstrap(database_url, migrate_only=True)
    monkeypatch.setattr(seed, "CHUNKS", ((*seed.CHUNKS[0][:-1], (0, 0, 0)),))
    with pytest.raises(psycopg.errors.CheckViolation):
        seed.bootstrap(database_url)
    with psycopg.connect(database_url) as connection:
        assert connection.execute("""
            SELECT (SELECT count(*) FROM vendors), (SELECT count(*) FROM price_catalog),
                   (SELECT count(*) FROM sop_policy_versions), (SELECT count(*) FROM sop_policy_rules),
                   (SELECT count(*) FROM sop_policy_chunks)
        """).fetchone() == (0, 0, 0, 0, 0)


def test_event_hash_and_foreign_key_constraints(database_url):
    seed.bootstrap(database_url, migrate_only=True)
    run_id, other_run = uuid4(), uuid4()
    digest = "a" * 64
    insert_event = "INSERT INTO processed_events (sha256, run_id) VALUES (%s, %s)"
    with psycopg.connect(database_url, autocommit=True) as connection:
        for identifier in (run_id, other_run):
            connection.execute("""
                INSERT INTO procurement_runs (run_id, thread_id, category, region)
                VALUES (%s, %s, 'procurement', 'US')
            """, (identifier, uuid4()))
        connection.execute(insert_event, (digest, run_id))
        with pytest.raises(psycopg.errors.UniqueViolation):
            connection.execute(insert_event, (digest, other_run))
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(insert_event, ("invalid", other_run))
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            connection.execute(insert_event, ("b" * 64, uuid4()))
        assert connection.execute(
            "SELECT run_id FROM processed_events WHERE sha256 = %s", (digest,)
        ).fetchone() == (run_id,)
