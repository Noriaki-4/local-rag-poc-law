import hashlib
import math

import requests

from .config import settings


def embed_text(
    text: str,
    dimension: int | None = None,
    *,
    timeout_sec: float | None = None,
) -> list[float]:
    return embed_texts([text], dimension, timeout_sec=timeout_sec)[0]


def embed_texts(
    texts: list[str],
    dimension: int | None = None,
    *,
    timeout_sec: float | None = None,
) -> list[list[float]]:
    expected_dimension = dimension or settings.embedding_dimension
    provider = settings.embedding_provider.lower()
    if provider == "ollama":
        return _ollama_embed_texts(
            texts,
            expected_dimension,
            timeout_sec=timeout_sec,
        )
    if provider in {"hash", "deterministic", "local"}:
        return [_hash_embed_text(text, expected_dimension) for text in texts]
    raise ValueError(f"Unsupported EMBEDDING_PROVIDER: {settings.embedding_provider}")


def _ollama_embed_texts(
    texts: list[str],
    expected_dimension: int,
    *,
    timeout_sec: float | None = None,
) -> list[list[float]]:
    if not texts:
        return []

    url = f"{settings.ollama_base_url.rstrip('/')}/api/embed"
    inputs = [_prepare_embedding_input(text) for text in texts]
    response = requests.post(
        url,
        json={"model": settings.embedding_model, "input": inputs, "truncate": True},
        timeout=timeout_sec or settings.embedding_timeout_sec,
    )
    if not response.ok:
        raise ValueError(f"Ollama embedding request failed: status={response.status_code} body={response.text[:500]}")
    embeddings = response.json().get("embeddings")
    if not isinstance(embeddings, list) or len(embeddings) != len(texts):
        raise ValueError(f"Ollama embedding response count mismatch: expected={len(texts)} actual={len(embeddings or [])}")

    return [_validate_embedding(vector, expected_dimension) for vector in embeddings]


def _prepare_embedding_input(text: str) -> str:
    normalized = " ".join(text.split())
    if not normalized:
        normalized = "empty"
    max_chars = max(settings.embedding_max_chars, 1)
    return normalized[:max_chars]


def _validate_embedding(vector: object, expected_dimension: int) -> list[float]:
    if not isinstance(vector, list):
        raise ValueError("Embedding vector must be a list")
    if len(vector) != expected_dimension:
        raise ValueError(f"Embedding dimension mismatch: expected={expected_dimension} actual={len(vector)}")
    normalized: list[float] = []
    for value in vector:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("Embedding vector contains a non-finite value")
        normalized.append(number)
    return normalized


def _hash_embed_text(text: str, dimension: int) -> list[float]:
    """Deterministic local embedding for smoke tests only."""
    vector = [0.0] * dimension
    tokens = [token for token in text.replace("\n", " ").split(" ") if token]
    if not tokens:
        tokens = ["empty"]

    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimension
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign

    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [round(value / norm, 8) for value in vector]
