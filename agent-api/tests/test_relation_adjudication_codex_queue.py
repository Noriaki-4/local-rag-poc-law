import asyncio
import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts/run_relation_adjudication_codex_queue.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "run_relation_adjudication_codex_queue", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_worker_contract_error_is_returned_to_same_worker_once(
    tmp_path, monkeypatch
) -> None:
    module = _load_module()
    program_calls = 0
    codex_calls = []

    async def run_program(*_args):
        nonlocal program_calls
        program_calls += 1
        if program_calls == 1:
            raise RuntimeError("unknown occurrenceHash")
        return ""

    async def run_codex(**kwargs):
        codex_calls.append(kwargs)
        return module.CodexResult("worker-thread", '{"records": []}', {"input": 1})

    class State:
        def __init__(self):
            self.value = {"shards": {"shard-1": {}}}

        def shard(self, shard_id):
            return dict(self.value["shards"].get(shard_id, {}))

        async def update(self, shard_id, **fields):
            self.value["shards"].setdefault(shard_id, {}).update(fields)

    monkeypatch.setattr(module, "_run_program", run_program)
    monkeypatch.setattr(module, "_run_codex", run_codex)
    state = State()
    raw = tmp_path / "worker-raw.jsonl"

    asyncio.run(
        module._bind_worker_with_contract_repair(
            packet_path=tmp_path / "packet.jsonl",
            raw_worker_path=raw,
            output_path=tmp_path / "worker.jsonl",
            worker_thread_id="worker-thread",
            model="gpt-test",
            reasoning_effort="high",
            worker_schema=tmp_path / "schema.json",
            session_workdir=tmp_path,
            semaphore=asyncio.Semaphore(1),
            state=state,
            shard_id="shard-1",
            count_field="initialWorkerContractRepairCount",
            usage_field="initialWorkerRepairUsage",
        )
    )

    assert program_calls == 2
    assert len(codex_calls) == 1
    assert codex_calls[0]["thread_id"] == "worker-thread"
    assert "意味判断を推測・補正しません" in codex_calls[0]["prompt"]
    assert state.shard("shard-1")["initialWorkerContractRepairCount"] == 1
    assert raw.read_text(encoding="utf-8") == ""
