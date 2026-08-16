"""DBを前提にしない、小さいCaseStore契約。"""

from typing import Protocol

from .state import CaseState


class CaseStoreError(RuntimeError):
    pass


class CaseNotFoundError(CaseStoreError):
    pass


class DuplicateCaseError(CaseStoreError):
    pass


class CaseStore(Protocol):
    def create(self, state: CaseState) -> None: ...

    def load(self, case_id: str) -> CaseState: ...

    def save(self, state: CaseState) -> None: ...
