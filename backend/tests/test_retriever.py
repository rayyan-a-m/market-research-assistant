from __future__ import annotations

import numpy as np

from app.pipeline.retriever import chunk_text, cosine_topk, order_blocks_primacy_recency


def test_chunk_text_overlap_and_coverage() -> None:
    text = "abcdefghij" * 10  # 100 chars
    chunks = chunk_text(text, size=40, overlap=10)
    assert len(chunks) >= 3
    # every chunk within size bound
    assert all(len(c) <= 40 for c in chunks)
    # overlap means consecutive chunks share a boundary region
    assert chunks[0][-10:] == chunks[1][:10]


def test_chunk_text_empty() -> None:
    assert chunk_text("", size=100, overlap=10) == []


def test_cosine_topk_orders_by_similarity() -> None:
    # matrix rows: identical, orthogonal, opposite to the query
    query = np.array([1.0, 0.0], dtype=np.float32)
    matrix = np.array([[1.0, 0.0], [0.0, 1.0], [-1.0, 0.0]], dtype=np.float32)
    top = cosine_topk(query, matrix, k=3)
    assert [idx for idx, _ in top] == [0, 1, 2]
    assert top[0][1] > top[1][1] > top[2][1]


def test_cosine_topk_respects_k() -> None:
    query = np.array([1.0, 0.0], dtype=np.float32)
    matrix = np.array([[1.0, 0.0], [0.9, 0.1], [0.0, 1.0]], dtype=np.float32)
    assert len(cosine_topk(query, matrix, k=2)) == 2


def test_cosine_topk_empty_matrix() -> None:
    query = np.array([1.0, 0.0], dtype=np.float32)
    assert cosine_topk(query, np.zeros((0, 2), dtype=np.float32), k=3) == []


def test_primacy_recency_ordering_places_top_first_second_last() -> None:
    scored = [
        ("a", 0.9, "block-a"),
        ("b", 0.8, "block-b"),
        ("c", 0.7, "block-c"),
        ("d", 0.6, "block-d"),
    ]
    ordered = order_blocks_primacy_recency(scored)
    urls = [u for u, _ in ordered]
    assert urls[0] == "a"  # highest relevance first (primacy)
    assert urls[-1] == "b"  # second-highest last (recency)
    assert urls[1:-1] == ["c", "d"]  # remainder in the middle, desc


def test_primacy_recency_small_input_sorts_desc() -> None:
    scored = [("a", 0.5, "A"), ("b", 0.9, "B")]
    ordered = order_blocks_primacy_recency(scored)
    assert [u for u, _ in ordered] == ["b", "a"]
