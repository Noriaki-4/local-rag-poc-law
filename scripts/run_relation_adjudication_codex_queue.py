"""Codex subscription sessionsでRelation意味分類shardを再開可能に処理する。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Awaitable
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parents[1]
PROJECT_PYTHON = REPO_ROOT / "agent-api" / ".venv" / "bin" / "python"


def _project_python() -> str:
    return str(PROJECT_PYTHON if PROJECT_PYTHON.exists() else Path(sys.executable))


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


@dataclass(frozen=True)
class RunPaths:
    root: Path

    @property
    def input(self) -> Path:
        return self.root / "input"

    @property
    def manifest(self) -> Path:
        return self.input / "manifest.json"

    @property
    def packet(self) -> Path:
        return self.root / "packet.jsonl"

    @property
    def state(self) -> Path:
        return self.root / "orchestration" / "state.json"

    def artifact(self, directory: str, shard_id: str) -> Path:
        return self.root / directory / f"{shard_id}.jsonl"


class QueueState:
    def __init__(self, path: Path, *, manifest_sha256: str, run_id: str) -> None:
        self.path = path
        self.lock = asyncio.Lock()
        if path.exists():
            self.value = _load_json(path)
            if self.value.get("manifestSha256") != manifest_sha256:
                raise ValueError("orchestration state belongs to another manifest")
            if self.value.get("classificationRunId") != run_id:
                raise ValueError("orchestration state belongs to another ClassificationRun")
        else:
            self.value = {
                "schemaVersion": 1,
                "manifestSha256": manifest_sha256,
                "classificationRunId": run_id,
                "createdAt": _utc_now(),
                "updatedAt": _utc_now(),
                "shards": {},
            }

    async def update(self, shard_id: str, **fields: Any) -> None:
        async with self.lock:
            shard = self.value["shards"].setdefault(shard_id, {})
            shard.update(fields)
            shard["updatedAt"] = _utc_now()
            self.value["updatedAt"] = _utc_now()
            _atomic_write_json(self.path, self.value)

    def shard(self, shard_id: str) -> dict[str, Any]:
        return dict(self.value["shards"].get(shard_id, {}))


@dataclass(frozen=True)
class CodexResult:
    thread_id: str
    last_message: str
    usage: dict[str, Any]


def _contract_text(*, reviewer: bool) -> str:
    skill_root = REPO_ROOT / ".agents" / "skills" / "legal-relation-adjudicator"
    parts = [
        (skill_root / "SKILL.md").read_text(encoding="utf-8"),
        (skill_root / "references" / "classification-contract.md").read_text(
            encoding="utf-8"
        ),
    ]
    if reviewer:
        parts.append(
            (skill_root / "references" / "review-contract.md").read_text(
                encoding="utf-8"
            )
        )
    return "\n\n--- NEXT CONTRACT DOCUMENT ---\n\n".join(parts)


def _write_structured_records(message: str, output: Path) -> None:
    try:
        value = json.loads(message)
        records = value["records"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise ValueError("Codex structured output does not contain records") from error
    if not isinstance(records, list):
        raise ValueError("Codex structured output records must be a list")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )


async def _ensure_output_schemas(paths: RunPaths) -> tuple[Path, Path]:
    schema_root = paths.root / "orchestration" / "schemas"
    worker_schema = schema_root / "worker-batch.schema.json"
    reviewer_schema = schema_root / "reviewer-batch.schema.json"
    schema_root.mkdir(parents=True, exist_ok=True)
    code = """
import json
import sys
from pathlib import Path
from pydantic import create_model
sys.path.insert(0, str(Path.cwd() / 'agent-api'))
from app.domains.legal.relation_classification import WorkerAdjudicationRecord, ReviewerRecord
def strict(node):
    if isinstance(node, dict):
        if node.get('type') == 'object' and isinstance(node.get('properties'), dict):
            node['required'] = list(node['properties'])
            node['additionalProperties'] = False
        for value in node.values():
            strict(value)
    elif isinstance(node, list):
        for value in node:
            strict(value)
for path, name, record in (
    (Path(sys.argv[1]), 'WorkerBatch', WorkerAdjudicationRecord),
    (Path(sys.argv[2]), 'ReviewerBatch', ReviewerRecord),
):
    batch = create_model(name, records=(list[record], ...))
    schema = batch.model_json_schema()
    strict(schema)
    path.write_text(json.dumps(schema, ensure_ascii=False), encoding='utf-8')
"""
    await _run_program(
        _project_python(),
        "-c",
        code,
        str(worker_schema),
        str(reviewer_schema),
    )
    return worker_schema, reviewer_schema


async def _run_codex(
    *,
    prompt: str,
    model: str,
    reasoning_effort: str,
    output_schema: Path,
    session_workdir: Path,
    thread_id: str | None = None,
    on_thread_started: Callable[[str], Awaitable[None]] | None = None,
) -> CodexResult:
    if thread_id is None:
        command = [
            "codex",
            "exec",
            "--json",
            "-m",
            model,
            "-c",
            f'model_reasoning_effort="{reasoning_effort}"',
            "-c",
            'approval_policy="never"',
            "-s",
            "read-only",
            "--skip-git-repo-check",
            "-C",
            str(session_workdir),
            "--output-schema",
            str(output_schema),
            "-",
        ]
    else:
        command = [
            "codex",
            "exec",
            "resume",
            "--json",
            "-m",
            model,
            "-c",
            f'model_reasoning_effort="{reasoning_effort}"',
            "--output-schema",
            str(output_schema),
            thread_id,
            "-",
        ]

    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=session_workdir,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=16 * 1024 * 1024,
    )
    assert process.stdout is not None
    assert process.stdin is not None
    process.stdin.write(prompt.encode("utf-8"))
    await process.stdin.drain()
    process.stdin.close()
    observed_thread_id = thread_id
    last_message = ""
    usage: dict[str, Any] = {}
    event_errors: list[str] = []
    async for raw_line in process.stdout:
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "thread.started":
            observed_thread_id = str(event["thread_id"])
            if on_thread_started is not None:
                await on_thread_started(observed_thread_id)
        if event.get("type") == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message":
                last_message = str(item.get("text") or "")
        if event.get("type") == "turn.completed":
            usage = dict(event.get("usage") or {})
        if event.get("type") in {"error", "turn.failed"}:
            event_errors.append(json.dumps(event, ensure_ascii=False))

    stderr = (await process.stderr.read()).decode("utf-8", errors="replace")
    return_code = await process.wait()
    if return_code != 0:
        raise RuntimeError(
            f"Codex session failed with exit {return_code}: "
            f"{'; '.join(event_errors)[-4000:]} {stderr[-2000:]}"
        )
    if observed_thread_id is None:
        raise RuntimeError("Codex session did not report a thread ID")
    return CodexResult(observed_thread_id, last_message, usage)


def _worker_prompt(paths: RunPaths, shard_id: str) -> str:
    packet = paths.artifact("input", shard_id).read_text(encoding="utf-8")
    return f"""以下はリポジトリ固有skillと分類契約の完全な本文です。これを今回の唯一の指示として適用してください。

<contract>
{_contract_text(reviewer=False)}
</contract>

あなたは意味分類Workerです。続くlabel-free packetの各候補を独立に読み、5 predicateを候補ごとに一括評価してください。gold・fixture・過去評価はありません。法的意味はあなた自身が判断してください。外部ツール、ファイル、Web、プログラムを使わず、指定schemaのrecordsだけを返してください。Neo4j/OpenSearchは変更しません。

<packet-jsonl>
{packet}
</packet-jsonl>"""


def _reviewer_prompt(paths: RunPaths, shard_id: str) -> str:
    packet = paths.artifact("input", shard_id).read_text(encoding="utf-8")
    worker = paths.artifact("worker", shard_id).read_text(encoding="utf-8")
    return f"""以下はリポジトリ固有skill・分類契約・Review契約の完全な本文です。これを今回の唯一の指示として適用してください。

<contract>
{_contract_text(reviewer=True)}
</contract>

あなたはWorkerと別contextの独立Reviewerです。原入力とWorker回答を照合し、全candidate/predicate、条件代数、意味方向、grounding、ID/span整合を監査してください。gold・fixture・過去評価はありません。外部ツール、ファイル、Web、プログラムを使わず、指定schemaのrecordsだけを返してください。

<packet-jsonl>
{packet}
</packet-jsonl>

<worker-jsonl>
{worker}
</worker-jsonl>"""


def _worker_revision_prompt(paths: RunPaths, shard_id: str) -> str:
    revision = paths.artifact("revision-input", shard_id).read_text(encoding="utf-8")
    return f"""同じshardについて1回限りの修正を行ってください。次のrequest_change候補だけが対象です。指摘を鵜呑みにせず、先の原本文で再検証し、各候補について全5 predicateを含む完全な修正版を指定schemaのrecordsだけで返してください。外部ツールは使わないでください。

<revision-jsonl>
{revision}
</revision-jsonl>"""


def _reviewer_final_prompt(paths: RunPaths, shard_id: str) -> str:
    revision = paths.artifact("revision-input", shard_id).read_text(encoding="utf-8")
    revised = paths.artifact("worker-revised", shard_id).read_text(encoding="utf-8")
    return f"""同じshardの最終差分レビューを行ってください。次の差し戻し対象と修正版だけを確認し、approveまたは最終request_changeを指定schemaのrecordsだけで返してください。差し戻しはこれ以上繰り返しません。外部ツールは使わないでください。

<revision-jsonl>
{revision}
</revision-jsonl>

<revised-worker-jsonl>
{revised}
</revised-worker-jsonl>"""


def _reviewer_contract_repair_prompt(validation_error: str) -> str:
    return f"""直前のReview出力は次の機械契約エラーで拒否されました。

<validation-error>
{validation_error[-3000:]}
</validation-error>

先に渡された原入力とWorker回答を再確認し、入力に実在する既知IDだけを文字単位でコピーしてください。ProgramはIDを推測・補正しません。全候補を含む完全なReview recordsを指定schemaで再出力してください。意味判断を変更する場合も、あなた自身が原本文で妥当と確認した場合だけにしてください。これは契約修復の唯一の再試行です。"""


def _worker_contract_repair_prompt(validation_error: str) -> str:
    return f"""直前のWorker出力は次の機械契約エラーで拒否されました。

<validation-error>
{validation_error[-3000:]}
</validation-error>

先に渡された入力を再確認し、candidateKey、referenceOccurrenceHash、Article ID、span IDは入力に実在する値だけを文字単位でコピーしてください。全候補・全5 predicateを含む完全なWorker recordsを指定schemaで再出力してください。ProgramはID、件数、predicate、意味判断を推測・補正しません。意味判断を変更する場合も、あなた自身が原本文で妥当と確認した場合だけにしてください。これは契約形式修復の唯一の再試行であり、Reviewerによる意味差し戻し回数には数えません。"""


async def _run_program(*args: str) -> str:
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=REPO_ROOT,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(
            f"command failed ({process.returncode}): {' '.join(args)}\n"
            f"{stdout.decode(errors='replace')[-3000:]}\n"
            f"{stderr.decode(errors='replace')[-3000:]}"
        )
    return stdout.decode("utf-8", errors="replace")


async def _with_session(
    semaphore: asyncio.Semaphore,
    operation: Callable[[], Awaitable[Any]],
) -> Any:
    async with semaphore:
        return await operation()


async def _bind_worker_with_contract_repair(
    *,
    packet_path: Path,
    raw_worker_path: Path,
    output_path: Path,
    worker_thread_id: str | None,
    model: str,
    reasoning_effort: str,
    worker_schema: Path,
    session_workdir: Path,
    semaphore: asyncio.Semaphore,
    state: QueueState,
    shard_id: str,
    count_field: str,
    usage_field: str,
) -> None:
    bind_command = (
        _project_python(),
        str(
            REPO_ROOT
            / ".agents/skills/legal-relation-adjudicator/scripts/bind_single_occurrence_ids.py"
        ),
        "--packet",
        str(packet_path),
        "--worker",
        str(raw_worker_path),
        "--output",
        str(output_path),
    )
    try:
        await _run_program(*bind_command)
        return
    except RuntimeError as error:
        repair_count = int(state.shard(shard_id).get(count_field) or 0)
        if not worker_thread_id or repair_count >= 1:
            raise
        result = await _with_session(
            semaphore,
            lambda: _run_codex(
                prompt=_worker_contract_repair_prompt(str(error)),
                model=model,
                reasoning_effort=reasoning_effort,
                output_schema=worker_schema,
                session_workdir=session_workdir,
                thread_id=worker_thread_id,
            ),
        )
        _write_structured_records(result.last_message, raw_worker_path)
        await state.update(
            shard_id,
            **{
                "stage": "worker_contract_repaired",
                count_field: repair_count + 1,
                usage_field: result.usage,
            },
        )
        await _run_program(*bind_command)


async def _process_shard(
    *,
    paths: RunPaths,
    shard_id: str,
    state: QueueState,
    semaphore: asyncio.Semaphore,
    model: str,
    reviewer_model: str,
    reasoning_effort: str,
    worker_schema: Path,
    reviewer_schema: Path,
    session_workdir: Path,
    run_id: str,
    apply: bool,
) -> None:
    await state.update(shard_id, stage="started")
    worker_path = paths.artifact("worker", shard_id)
    if not worker_path.exists():
        raw_worker_path = paths.artifact("worker-raw", shard_id)
        if not raw_worker_path.exists():
            result = await _with_session(
                semaphore,
                lambda: _run_codex(
                    prompt=_worker_prompt(paths, shard_id),
                    model=model,
                    reasoning_effort=reasoning_effort,
                    output_schema=worker_schema,
                    session_workdir=session_workdir,
                    on_thread_started=lambda thread_id: state.update(
                        shard_id, workerThreadId=thread_id
                    ),
                ),
            )
            _write_structured_records(result.last_message, raw_worker_path)
            await state.update(
                shard_id,
                workerThreadId=result.thread_id,
                workerUsage=result.usage,
            )
        worker_thread_id = state.shard(shard_id).get("workerThreadId")
        await _bind_worker_with_contract_repair(
            packet_path=paths.artifact("input", shard_id),
            raw_worker_path=raw_worker_path,
            output_path=worker_path,
            worker_thread_id=str(worker_thread_id) if worker_thread_id else None,
            model=model,
            reasoning_effort=reasoning_effort,
            worker_schema=worker_schema,
            session_workdir=session_workdir,
            semaphore=semaphore,
            state=state,
            shard_id=shard_id,
            count_field="initialWorkerContractRepairCount",
            usage_field="initialWorkerRepairUsage",
        )
        await state.update(
            shard_id,
            stage="worker_complete",
        )
    if not worker_path.exists():
        raise RuntimeError(f"Worker did not create {worker_path}")

    review_path = paths.artifact("review-initial", shard_id)
    if not review_path.exists():
        result = await _with_session(
            semaphore,
            lambda: _run_codex(
                prompt=_reviewer_prompt(paths, shard_id),
                model=reviewer_model,
                reasoning_effort=reasoning_effort,
                output_schema=reviewer_schema,
                session_workdir=session_workdir,
                on_thread_started=lambda thread_id: state.update(
                    shard_id, reviewerThreadId=thread_id
                ),
            ),
        )
        _write_structured_records(result.last_message, review_path)
        await state.update(
            shard_id,
            stage="initial_review_complete",
            reviewerThreadId=result.thread_id,
            reviewerUsage=result.usage,
        )
    if not review_path.exists():
        raise RuntimeError(f"Reviewer did not create {review_path}")

    revision_input = paths.artifact("revision-input", shard_id)
    prepare_command = (
        _project_python(),
        str(
            REPO_ROOT
            / ".agents/skills/legal-relation-adjudicator/scripts/prepare_revision_packet.py"
        ),
        "--packet",
        str(paths.artifact("input", shard_id)),
        "--worker",
        str(worker_path),
        "--review",
        str(review_path),
        "--output",
        str(revision_input),
    )
    try:
        await _run_program(*prepare_command)
    except RuntimeError as error:
        shard_state = state.shard(shard_id)
        reviewer_thread = shard_state.get("reviewerThreadId")
        repair_count = int(shard_state.get("initialReviewContractRepairCount") or 0)
        if not reviewer_thread or repair_count >= 1:
            raise
        result = await _with_session(
            semaphore,
            lambda: _run_codex(
                prompt=_reviewer_contract_repair_prompt(str(error)),
                model=reviewer_model,
                reasoning_effort=reasoning_effort,
                output_schema=reviewer_schema,
                session_workdir=session_workdir,
                thread_id=str(reviewer_thread),
            ),
        )
        _write_structured_records(result.last_message, review_path)
        await state.update(
            shard_id,
            stage="initial_review_contract_repaired",
            initialReviewContractRepairCount=repair_count + 1,
            initialReviewRepairUsage=result.usage,
        )
        await _run_program(*prepare_command)
    revision_count = len(_load_jsonl(revision_input))
    shard_state = state.shard(shard_id)
    if revision_count:
        worker_thread = shard_state.get("workerThreadId")
        reviewer_thread = shard_state.get("reviewerThreadId")
        revised_path = paths.artifact("worker-revised", shard_id)
        if not revised_path.exists():
            if not worker_thread:
                raise RuntimeError(
                    f"{shard_id} needs Worker revision but its session ID is unavailable"
                )
            result = await _with_session(
                semaphore,
                lambda: _run_codex(
                    prompt=_worker_revision_prompt(paths, shard_id),
                    model=model,
                    reasoning_effort=reasoning_effort,
                    output_schema=worker_schema,
                    session_workdir=session_workdir,
                    thread_id=str(worker_thread),
                ),
            )
            raw_revised_path = paths.artifact("worker-revised-raw", shard_id)
            _write_structured_records(result.last_message, raw_revised_path)
            await _bind_worker_with_contract_repair(
                packet_path=revision_input,
                raw_worker_path=raw_revised_path,
                output_path=revised_path,
                worker_thread_id=str(worker_thread),
                model=model,
                reasoning_effort=reasoning_effort,
                worker_schema=worker_schema,
                session_workdir=session_workdir,
                semaphore=semaphore,
                state=state,
                shard_id=shard_id,
                count_field="revisedWorkerContractRepairCount",
                usage_field="revisedWorkerRepairUsage",
            )
            await state.update(
                shard_id,
                stage="worker_revision_complete",
                workerRevisionUsage=result.usage,
            )
        final_review = paths.artifact("review-final", shard_id)
        if not final_review.exists():
            if not reviewer_thread:
                raise RuntimeError(
                    f"{shard_id} needs final review but its Reviewer session ID is unavailable"
                )
            result = await _with_session(
                semaphore,
                lambda: _run_codex(
                    prompt=_reviewer_final_prompt(paths, shard_id),
                    model=reviewer_model,
                    reasoning_effort=reasoning_effort,
                    output_schema=reviewer_schema,
                    session_workdir=session_workdir,
                    thread_id=str(reviewer_thread),
                ),
            )
            _write_structured_records(result.last_message, final_review)
            await state.update(
                shard_id,
                stage="final_review_complete",
                reviewerFinalUsage=result.usage,
            )
    else:
        revised_path = revision_input
        final_review = revision_input

    approved = paths.artifact("approved", shard_id)
    unresolved = paths.artifact("unresolved", shard_id)
    await _run_program(
        _project_python(),
        str(
            REPO_ROOT
            / ".agents/skills/legal-relation-adjudicator/scripts/merge_reviewed_results.py"
        ),
        "--packet",
        str(paths.artifact("input", shard_id)),
        "--initial-worker",
        str(worker_path),
        "--initial-review",
        str(review_path),
        "--revised-worker",
        str(revised_path),
        "--final-review",
        str(final_review),
        "--approved-output",
        str(approved),
        "--unresolved-output",
        str(unresolved),
    )
    await state.update(
        shard_id,
        stage="merged",
        revisionCount=revision_count,
        approvedCount=len(_load_jsonl(approved)),
        unresolvedCount=len(_load_jsonl(unresolved)),
    )

    if apply:
        await _run_program(
            _project_python(),
            str(REPO_ROOT / "scripts/import_relation_adjudication_results.py"),
            "--manifest",
            str(paths.manifest),
            "--packet",
            str(paths.packet),
            "--approved",
            str(approved),
            "--unresolved",
            str(unresolved),
            "--run-id",
            run_id,
            "--apply",
        )
        await state.update(shard_id, stage="imported", error=None)


async def _run(args: argparse.Namespace) -> int:
    paths = RunPaths(args.run_root.resolve())
    manifest = _load_json(paths.manifest)
    execution = manifest["executionProfile"]
    configured_max = int(execution["maxActiveSessions"])
    if not 1 <= args.max_active_sessions <= configured_max:
        raise ValueError(
            f"--max-active-sessions must be between 1 and {configured_max}"
        )
    if int(execution["maxRevisionRounds"]) != 1:
        raise ValueError("only one revision round is supported")
    if not bool(execution["workerReviewerSeparateContexts"]):
        raise ValueError("manifest must require separate Worker/Reviewer contexts")

    import hashlib

    state = QueueState(
        paths.state,
        manifest_sha256=hashlib.sha256(paths.manifest.read_bytes()).hexdigest(),
        run_id=args.run_id,
    )
    pending = []
    for shard in manifest["shards"]:
        shard_id = shard["shardId"]
        artifacts_complete = paths.artifact("approved", shard_id).exists() and paths.artifact(
            "unresolved", shard_id
        ).exists()
        if artifacts_complete and (
            not args.apply or state.shard(shard_id).get("stage") == "imported"
        ):
            continue
        pending.append(shard_id)
    if args.max_shards is not None:
        pending = pending[: args.max_shards]
    if not args.execute:
        print(
            json.dumps(
                {
                    "pendingShardCount": len(pending),
                    "firstShardIds": pending[:10],
                    "maxActiveSessions": args.max_active_sessions,
                    "wouldImport": args.apply,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    semaphore = asyncio.Semaphore(args.max_active_sessions)
    worker_schema, reviewer_schema = await _ensure_output_schemas(paths)
    session_workdir = paths.root / "orchestration" / "session-work"
    session_workdir.mkdir(parents=True, exist_ok=True)

    async def guarded(shard_id: str) -> tuple[str, str | None]:
        try:
            await _process_shard(
                paths=paths,
                shard_id=shard_id,
                state=state,
                semaphore=semaphore,
                model=str(execution["workerModel"]),
                reviewer_model=str(execution["reviewerModel"]),
                reasoning_effort=str(execution["reasoningEffort"]),
                worker_schema=worker_schema,
                reviewer_schema=reviewer_schema,
                session_workdir=session_workdir,
                run_id=args.run_id,
                apply=args.apply,
            )
            return shard_id, None
        except Exception as error:  # preserve other shards and resume state
            await state.update(shard_id, stage="failed", error=str(error))
            return shard_id, str(error)

    queue: asyncio.Queue[str] = asyncio.Queue()
    for shard_id in pending:
        queue.put_nowait(shard_id)
    results: list[tuple[str, str | None]] = []
    results_lock = asyncio.Lock()
    failure_lock = asyncio.Lock()
    stop = asyncio.Event()
    consecutive_failures = 0

    async def consume() -> None:
        nonlocal consecutive_failures
        while not stop.is_set():
            try:
                shard_id = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            result = await guarded(shard_id)
            async with results_lock:
                results.append(result)
            async with failure_lock:
                if result[1] is None:
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    if consecutive_failures >= args.max_consecutive_failures:
                        stop.set()
            print(
                json.dumps(
                    {
                        "shardId": result[0],
                        "status": "completed" if result[1] is None else "failed",
                        "error": result[1],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            queue.task_done()

    await asyncio.gather(
        *(consume() for _ in range(min(args.max_active_sessions, len(pending))))
    )
    failures = {shard_id: error for shard_id, error in results if error}
    print(
        json.dumps(
            {
                "selectedShardCount": len(pending),
                "attemptedShardCount": len(results),
                "completedShardCount": len(results) - len(failures),
                "remainingShardCount": queue.qsize(),
                "failedShards": failures,
                "state": str(paths.state),
                "published": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--max-active-sessions", type=int, default=3)
    parser.add_argument("--max-consecutive-failures", type=int, default=3)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--max-shards", type=int)
    scope.add_argument("--all-shards", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--apply", action="store_true", help="checkpointをNeo4jへ保存する（publishしない）"
    )
    args = parser.parse_args()
    if args.max_shards is not None and args.max_shards < 1:
        parser.error("--max-shards must be positive")
    if args.max_consecutive_failures < 1:
        parser.error("--max-consecutive-failures must be positive")
    if args.apply and not args.execute:
        parser.error("--apply requires --execute")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
