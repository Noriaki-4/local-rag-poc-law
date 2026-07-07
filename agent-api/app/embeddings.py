import hashlib
import math


def embed_text(text: str, dimension: int = 1024) -> list[float]:
    """Deterministic local embedding for Docker smoke tests.

    This is intentionally not a legal-quality embedding model. It lets the
    local stack exercise OpenSearch kNN/Hybrid wiring before Phase 0 model
    selection replaces it with a real embedding provider.
    """
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
