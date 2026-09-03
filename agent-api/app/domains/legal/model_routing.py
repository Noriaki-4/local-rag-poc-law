"""法令AgentのLLM処理とmodel levelの対応を解決する。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, cast

from app.config import settings

ModelLevel = Literal["low", "middle", "high"]

_MODEL_LEVELS_PATH = Path(__file__).with_name("model_levels.json")
_EXPECTED_PURPOSES = frozenset(
    {
        "question_readiness",
        "question_decomposition",
        "hypothesis_generation",
        "hypothesis_revision",
        "search_planning",
        "integration",
        "evidence_integration",
        "dependency_assessment",
        "cycle_close",
        "finalization",
        "reviewer_revision",
        "search_review",
        "graph_review",
        "reviewer",
        "post_run_audit",
    }
)


def _load_model_levels() -> dict[str, ModelLevel]:
    payload = json.loads(_MODEL_LEVELS_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) != _EXPECTED_PURPOSES:
        raise ValueError("model_levels.json must define every legal LLM purpose once")
    invalid = {
        purpose: level
        for purpose, level in payload.items()
        if level not in {"low", "middle", "high"}
    }
    if invalid:
        raise ValueError(f"model_levels.json contains invalid levels: {invalid}")
    return {purpose: cast(ModelLevel, level) for purpose, level in payload.items()}


MODEL_LEVELS = _load_model_levels()


def legal_model_level_for(purpose: str) -> ModelLevel:
    try:
        return MODEL_LEVELS[purpose]
    except KeyError as exc:
        raise KeyError(f"unknown legal LLM purpose: {purpose}") from exc


def legal_model_for(purpose: str) -> str:
    return settings.agent_framework_model_for_level(legal_model_level_for(purpose))


def legal_model_routing() -> dict[str, dict[str, str]]:
    return {
        purpose: {
            "level": level,
            "model": settings.agent_framework_model_for_level(level),
        }
        for purpose, level in MODEL_LEVELS.items()
    }
