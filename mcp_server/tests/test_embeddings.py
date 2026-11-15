import asyncio
import json

import httpx
import pytest

from mcp_server import embeddings


def test_fixture_uses_seeded_topic_dimensions():
    embedder = embeddings.FixtureEmbedder()
    vector = asyncio.run(embedder.embed("Finance Director approval cap"))
    assert len(vector) == embeddings.DIMENSIONS
    assert vector[:3] == [0, 1, 0]
    assert embedder.space == "procurement-fixture-v1"


@pytest.mark.parametrize("vector", [[0.0], [0.0] * 1536, [float("nan")] * 1536])
def test_invalid_vectors_cannot_reach_postgres(vector):
    with pytest.raises(embeddings.EmbeddingUnavailable):
        embeddings.vector_parameter(vector)


def test_openai_retries_only_transient_statuses(monkeypatch):
    statuses = iter([429, 503, 200, 401])
    calls = []

    def respond(request):
        calls.append(request)
        assert json.loads(request.content)["dimensions"] == embeddings.DIMENSIONS
        status = next(statuses)
        return httpx.Response(status, json={"data": [{"embedding": [1.0] + [0.0] * 1535}]})

    original_client = httpx.AsyncClient

    def mock_client(**options):
        return original_client(transport=httpx.MockTransport(respond), **options)

    monkeypatch.setattr(embeddings.httpx, "AsyncClient", mock_client)
    provider = embeddings.OpenAIEmbedder("test-key")
    assert len(asyncio.run(provider.embed("Approval cap"))) == embeddings.DIMENSIONS
    with pytest.raises(embeddings.EmbeddingUnavailable, match="provider unavailable"):
        asyncio.run(provider.embed("Approval cap"))
    assert len(calls) == 4
