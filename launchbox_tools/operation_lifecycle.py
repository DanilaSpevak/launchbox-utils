from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum


class OperationPhase(str, Enum):
    SCAN = "scan"
    STAGE = "stage"
    COMMIT = "commit"
    ROLLBACK = "rollback"
    FINALIZE = "finalize"
    FINISHED = "finished"


class OperationCancelled(Exception):
    """Raised when a cooperative operation is cancelled before commit."""


@dataclass(frozen=True)
class OperationSnapshot:
    phase: OperationPhase
    cancel_requested: bool
    commit_started: bool


class OperationControl:
    """Thread-safe phase tracking and cooperative pre-commit cancellation."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancel_event = threading.Event()
        self._phase = OperationPhase.SCAN
        self._commit_started = False
        self._finalize_started = False

    def snapshot(self) -> OperationSnapshot:
        with self._lock:
            return OperationSnapshot(
                phase=self._phase,
                cancel_requested=self._cancel_event.is_set(),
                commit_started=self._commit_started,
            )

    def set_phase(self, phase: OperationPhase) -> None:
        with self._lock:
            self._phase = phase

    def request_cancel(self) -> bool:
        with self._lock:
            if self._commit_started or self._finalize_started or self._phase not in {
                OperationPhase.SCAN,
                OperationPhase.STAGE,
            }:
                return False
            self._cancel_event.set()
            return True

    def checkpoint(self) -> None:
        with self._lock:
            if self._cancel_event.is_set() and not self._commit_started:
                raise OperationCancelled("Operation cancelled")

    def begin_commit(self) -> None:
        """Atomically reject cancellation or make commit irreversible."""

        with self._lock:
            if self._cancel_event.is_set():
                raise OperationCancelled("Operation cancelled")
            self._commit_started = True
            self._phase = OperationPhase.COMMIT

    def begin_finalize(self) -> None:
        """Atomically close cancellable work and enter finalization."""

        with self._lock:
            cancelled = self._cancel_event.is_set() and not self._commit_started
            self._finalize_started = True
            self._phase = OperationPhase.FINALIZE
            if cancelled:
                raise OperationCancelled("Operation cancelled")

    def finish(self) -> None:
        self.set_phase(OperationPhase.FINISHED)
