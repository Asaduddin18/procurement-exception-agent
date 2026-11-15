import asyncio
from datetime import date
from decimal import Decimal

import psycopg
import pytest

from mcp_server.embeddings import FixtureEmbedder, vector_parameter
from mcp_server.repository import EvidenceRepository, EvidenceUnavailable

EVALUATION_DATE = date(2026, 10, 19)
FIXTURE = FixtureEmbedder()


def test_catalog_is_exact_and_vendor_scoped(reader_url):
    repository = EvidenceRepository(reader_url)

    async def check():
        terms = await repository.vendor_terms("Apex Industrial Supply")
        assert terms is not None
        assert terms.contract_reference == "CTR-APX-2026-01"
        assert terms.payment_terms_days == 30
        assert await repository.vendor_terms("Apex") is None
        result = await repository.approved_prices(
            "Apex Industrial Supply", ["GSK-150", "SS-316 Valve", "ss-316 valve", "GSK-150"]
        )
        assert result.status == "partial"
        assert [(price.sku, price.unit_price) for price in result.prices] == [
            ("GSK-150", Decimal("40.00")), ("SS-316 Valve", Decimal("120.00"))
        ]
        assert result.missing_skus == ["ss-316 valve"]
        harbor = await repository.approved_prices("Harbor Flow Components", ["SS-316 Valve"])
        assert harbor.prices[0].unit_price == Decimal("128.00")
        missing = await repository.approved_prices("Apex Industrial Supply", ["SS-999"])
        assert missing.status == "sku_not_found"
        unknown = await repository.approved_prices("' OR true --", ["SS-316 Valve"])
        assert unknown.status == "vendor_not_found"
        assert unknown.missing_skus == ["SS-316 Valve"]

    asyncio.run(check())


def test_reader_has_database_enforced_permissions(reader_url):
    with psycopg.connect(reader_url, autocommit=True) as reader:
        assert reader.execute("SELECT count(*) FROM public.vendors").fetchone() == (3,)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            reader.execute("UPDATE public.vendors SET payment_terms_days = 0")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            reader.execute("SELECT * FROM public.procurement_inputs")


def test_postgres_error_does_not_look_like_no_matches(reader_url, database_url):
    with psycopg.connect(database_url) as owner:
        owner.execute("REVOKE SELECT ON public.vendors FROM procurement_mcp_reader")
    with pytest.raises(EvidenceUnavailable, match="evidence database unavailable"):
        asyncio.run(EvidenceRepository(reader_url).vendor_terms("Apex Industrial Supply"))


def test_pgvector_ranks_only_sql_eligible_policies(reader_url, database_url):
    embedding = vector_parameter(asyncio.run(FIXTURE.embed("Finance Director approval cap")))
    controls = (
        ("wrong-category", "US", "freight", FIXTURE.space),
        ("wrong-region", "EU", "procurement", FIXTURE.space),
        ("wrong-space", "US", "procurement", "different-model-v1"),
        ("same-day", "US", "procurement", FIXTURE.space),
    )
    with psycopg.connect(database_url) as writer:
        for policy_id, region, category, space in controls:
            writer.execute("""
                INSERT INTO public.sop_policy_versions
                    (policy_id, version, status, effective_date, category, region, title)
                VALUES (%s, 1, 'ACTIVE', %s, %s, %s, %s)
            """, (policy_id, EVALUATION_DATE, category, region, policy_id))
            writer.execute("""
                INSERT INTO public.sop_policy_chunks
                    (policy_id, version, chunk_number, source_location, content, embedding_space, embedding)
                VALUES (%s, 1, 1, 'Section 1', 'Control passage', %s, %s::vector)
            """, (policy_id, space, embedding))
        unscoped = writer.execute("""
            SELECT policy_id, version, embedding <=> %s::vector AS distance
            FROM public.sop_policy_chunks
            ORDER BY distance
            LIMIT 5
        """, (embedding,)).fetchall()
        assert all(distance == pytest.approx(0.0) for _, _, distance in unscoped)

    repository = EvidenceRepository(reader_url)
    search = asyncio.run(repository.search(
        "procurement", "US", EVALUATION_DATE, FIXTURE.space, embedding
    ))
    assert [(row.policy_id, row.version, row.chunk_number) for row in search] == [
        ("same-day", 1, 1), ("US-PROCUREMENT", 2026, 2), ("US-PROCUREMENT", 2026, 1),
        ("US-PROCUREMENT", 2026, 3),
    ]
    assert search[1].source_location == "Section 3.2, approval authority"
    assert "USD 10,000" in search[1].content
    assert search[1].cosine_distance > search[0].cosine_distance
    assert asyncio.run(repository.search(
        "manufacturing", "US", EVALUATION_DATE, FIXTURE.space, embedding
    )) == []
