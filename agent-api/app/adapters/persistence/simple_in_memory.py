"""新しい薄いCaseStore契約の、単一プロセス用InMemory実装。"""

from copy import deepcopy
from threading import RLock

from app.agent_framework.state import CaseState
from app.agent_framework.store import (
    CaseNotFoundError,
    DuplicateCaseError,
)


class InMemoryCaseStore:
    def __init__(self) -> None:
        self._states: dict[str, CaseState] = {}
        self._lock = RLock()

    def create(self, state: CaseState) -> None:
        with self._lock:
            if state.case_id in self._states:
                raise DuplicateCaseError(f"duplicate case: {state.case_id}")
            self._states[state.case_id] = deepcopy(state)

    def load(self, case_id: str) -> CaseState:
        with self._lock:
            try:
                return deepcopy(self._states[case_id])
            except KeyError as exc:
                raise CaseNotFoundError(f"case not found: {case_id}") from exc

    def save(self, state: CaseState) -> None:
        with self._lock:
            if state.case_id not in self._states:
                raise CaseNotFoundError(f"case not found: {state.case_id}")
            self._states[state.case_id] = deepcopy(state)
