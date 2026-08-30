"""WorkItemへ固定した論理Model sessionを割り当てる。"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from threading import RLock

from .context import SolverContext


@dataclass(frozen=True)
class WorkItemSession:
    """Provider会話状態に依存しない、Case内のWorkItem専属session。"""

    session_id: str
    work_item_id: str
    turn: int

    def as_input(self) -> dict[str, str | int]:
        return {
            "session_id": self.session_id,
            "work_item_id": self.work_item_id,
            "turn": self.turn,
        }


class WorkItemSessionCoordinator:
    """同じWorkItemへ同じsession IDと単調増加turnを割り当てる。"""

    def __init__(self) -> None:
        self._turns: dict[tuple[str, str], int] = {}
        self._lock = RLock()

    def assign(
        self,
        contexts: tuple[SolverContext, ...],
    ) -> tuple[tuple[WorkItemSession, SolverContext], ...]:
        keys = tuple(_session_key(context) for context in contexts)
        if len(keys) != len(set(keys)):
            raise ValueError("one batch cannot contain the same WorkItem session twice")

        assignments = []
        with self._lock:
            for context, key in zip(contexts, keys, strict=True):
                turn = self._turns.get(key, 0) + 1
                self._turns[key] = turn
                assignments.append(
                    (
                        WorkItemSession(
                            session_id=_session_id(*key),
                            work_item_id=key[1],
                            turn=turn,
                        ),
                        context,
                    )
                )
        return tuple(assignments)


def first_work_item_session(context: SolverContext) -> WorkItemSession:
    """単一WorkItemのレビュー用成果物へ安定した初回sessionを付ける。"""

    case_id, work_item_id = _session_key(context)
    return WorkItemSession(
        session_id=_session_id(case_id, work_item_id),
        work_item_id=work_item_id,
        turn=1,
    )


def _session_key(context: SolverContext) -> tuple[str, str]:
    if len(context.work_tree) != 1:
        raise ValueError("WorkItem session requires exactly one projected WorkItem")
    return context.case_id, context.work_tree[0].work_item_id


def _session_id(case_id: str, work_item_id: str) -> str:
    digest = sha256(f"{case_id}\0{work_item_id}".encode()).hexdigest()[:24]
    return f"work-item-session-{digest}"
