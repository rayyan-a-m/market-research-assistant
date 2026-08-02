from __future__ import annotations

from app.config import Settings
from app.pipeline.fetcher import FetchedSource
from app.pipeline.retriever import build_context, build_indexes
from tests.fakes import FakeEmbeddingProvider


def _settings() -> Settings:
    # _env_file=None → pure defaults, isolated from the local .env
    return Settings(_env_file=None)


def _source(url: str, word: str) -> FetchedSource:
    return FetchedSource(
        url=url, source_id=url, markdown=f"{word} content about the market. " * 300, origin="USER_SUPPLIED"
    )


async def test_build_indexes_embeds_each_source_and_shapes_match() -> None:
    emb = FakeEmbeddingProvider()
    sources = [_source("https://a.com", "Alpha"), _source("https://b.com", "Beta")]

    indexes = await build_indexes(sources, _settings(), emb)

    assert set(indexes) == {"https://a.com", "https://b.com"}
    for idx in indexes.values():
        # one embedding per chunk, and the long text produced multiple chunks
        assert idx.embeddings.shape[0] == len(idx.chunks) > 1


async def test_build_context_labels_each_source_and_appends_reminder() -> None:
    emb = FakeEmbeddingProvider()
    sources = [_source("https://a.com", "Alpha"), _source("https://b.com", "Beta")]
    indexes = await build_indexes(sources, _settings(), emb)

    ctx = await build_context(["payments", "finance"], indexes, _settings(), emb)

    assert "[Source: https://a.com]" in ctx
    assert "[Source: https://b.com]" in ctx
    assert "REMINDER" in ctx  # attribution rules repeated at the end (recency)


async def test_build_indexes_empty_sources() -> None:
    assert await build_indexes([], _settings(), FakeEmbeddingProvider()) == {}
