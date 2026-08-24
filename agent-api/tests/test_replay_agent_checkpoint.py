from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from app.agent_framework.contracts import SolverDecision
from app.agent_framework.ports.model import SolverCallResult


SCRIPT_PATH = Path(__file__).parents[2] / "scripts/replay_agent_checkpoint.py"
SPEC = importlib.util.spec_from_file_location("replay_agent_checkpoint", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _fixture() -> dict:
    path = (
        Path(__file__).parent
        / "fixtures/framework/tob_overview_initial_research_decomposition_v1.json"
    )
    fixture = json.loads(path.read_text(encoding="utf-8"))
    fixture["checkpoint"] = {
        "resumeFrom": "research",
        "approved": True,
        "approvedBy": "test",
        "sourceProvider": "anthropic",
        "sourceModel": "claude-haiku-test",
    }
    return fixture


def test_checkpoint_replay_requires_explicit_approval(tmp_path: Path) -> None:
    fixture = _fixture()
    fixture["checkpoint"]["approved"] = False
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps(fixture), encoding="utf-8")

    with pytest.raises(ValueError, match="explicitly approved"):
        MODULE._load_checkpoint(path)


def test_checkpoint_replay_uses_requested_provider_and_model(monkeypatch) -> None:
    fixture = _fixture()
    observed = {}

    class FakeClient:
        def __init__(self, *, provider: str) -> None:
            observed["provider"] = provider

    class FakeAdapter:
        def __init__(self, client) -> None:
            observed["client"] = client

        def solve(self, context, profile) -> SolverCallResult:
            observed["case_id"] = context.case_id
            observed["model"] = profile.model
            return SolverCallResult(
                decision=SolverDecision(
                    next="finalize",
                    answer={"text": "診断結果"},
                ),
                input_tokens=10,
                output_tokens=5,
                attempt_count=1,
            )

    monkeypatch.setattr(MODULE, "LLMClient", FakeClient)
    monkeypatch.setattr(MODULE, "StructuredJSONModelAdapter", FakeAdapter)

    output = MODULE.replay_checkpoint(
        fixture,
        provider="openai",
        model="gpt-4o-mini-2024-07-18",
        stage="research",
    )

    assert observed == {
        "provider": "openai",
        "client": observed["client"],
        "case_id": fixture["solverContext"]["case_id"],
        "model": "gpt-4o-mini-2024-07-18",
    }
    assert output["decision"]["answer"]["text"] == "診断結果"
    assert output["inputTokens"] == 10
