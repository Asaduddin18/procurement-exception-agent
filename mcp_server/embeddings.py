"""Embedding providers used only for SOP passage retrieval."""

import math
import re
from collections.abc import Sequence
from typing import ClassVar, Protocol

import httpx
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

DIMENSIONS = 1536


class EmbeddingUnavailable(RuntimeError):
    """The configured provider could not produce a valid embedding."""


class Embedder(Protocol):
    space: ClassVar[str]

    async def embed(self, text: str) -> Sequence[float]: ...


def vector_parameter(values: Sequence[float]) -> str:
    if (
        len(values) != DIMENSIONS
        or not all(math.isfinite(value) and abs(value) <= 1 for value in values)
        or math.hypot(*values) < 1e-6
    ):
        raise EmbeddingUnavailable("SOP embedding has an invalid shape or magnitude.")
    return "[" + ",".join(map(str, values)) + "]"


class FixtureEmbedder:
    """Project local demo queries onto the three seeded policy topics."""

    space = "procurement-fixture-v1"

    async def embed(self, text: str) -> list[float]:
        tokens = set(re.findall(r"[a-z]+", text.lower()))
        weights = [
            float(bool(tokens & {"payment", "terms", "net"})),
            float(bool(tokens & {"approval", "approve", "authority", "cap", "limit", "director"})),
            float(bool(tokens & {"variance", "overcharge", "price", "catalog"})),
        ]
        return [*(weights if any(weights) else [1.0, 1.0, 1.0]), *([0.0] * (DIMENSIONS - 3))]


def retryable_status(exc: BaseException) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and (
        exc.response.status_code == 429 or 500 <= exc.response.status_code <= 599
    )


class OpenAIEmbedder:
    model = "text-embedding-3-small"
    space = f"openai:{model}:{DIMENSIONS}"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    async def embed(self, text: str) -> list[float]:
        try:
            async with httpx.AsyncClient(
                timeout=8.0, headers={"Authorization": f"Bearer {self.api_key}"}
            ) as client:
                async for attempt in AsyncRetrying(
                    retry=retry_if_exception(retryable_status),
                    stop=stop_after_attempt(3),
                    wait=wait_exponential(multiplier=0.2, max=1.0),
                    reraise=True,
                ):
                    with attempt:
                        response = await client.post(
                            "https://api.openai.com/v1/embeddings",
                            json={"input": text, "model": self.model, "dimensions": DIMENSIONS},
                        )
                        response.raise_for_status()
                        vector = response.json()["data"][0]["embedding"]
                        if not isinstance(vector, list):
                            raise TypeError("Expected an embedding array.")
                        return [float(value) for value in vector]
        except (httpx.HTTPError, KeyError, ValueError, TypeError, IndexError, OverflowError):
            raise EmbeddingUnavailable("SOP embedding provider unavailable.") from None
        raise EmbeddingUnavailable("SOP embedding provider unavailable.")
