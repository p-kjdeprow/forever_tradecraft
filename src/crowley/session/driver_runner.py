"""Shared Guardian action lifecycle for desktop driver operations."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, TypeVar

from .guardian import GuardianError
from .guardian.correlation import ActionCorrelation

_ResultT = TypeVar("_ResultT")


class ActionLifecycle(Protocol):
    """The Guardian surface required by :class:`GuardedActionRunner`."""

    @property
    def armed(self) -> bool: ...

    @property
    def generation(self) -> int: ...

    def finish_action(self, generation: int, action: ActionCorrelation) -> None: ...


class GuardedActionRunner:
    """Run one action in the fixed begin/admit/emit/wait/finish template.

    The operation callback owns broker claims and native emission.  This is
    deliberate: broker work remains between Guardian admission and event
    completion, while the lifecycle ordering is centralized here.
    """

    def __init__(self, guardian: ActionLifecycle, revoke: Callable[[str], object]) -> None:
        self._guardian: ActionLifecycle = guardian
        self._revoke = revoke

    def run(
        self,
        *,
        generation: int,
        begin: Callable[[], ActionCorrelation],
        dispatch: Callable[[], None],
        operation: Callable[[ActionCorrelation], _ResultT],
        wait: Callable[[ActionCorrelation], None],
        failure_reason: Callable[[Exception], str],
        guardian_failure_reason: Callable[[GuardianError], str] | None,
        revoke_on_failure: bool = False,
        finish_when_current: bool = False,
        on_finish: Callable[[bool, Exception | None], None] | None = None,
        raise_finish_error: bool = True,
    ) -> _ResultT:
        """Execute the lifecycle and return the operation result."""

        try:
            action = begin()
        except GuardianError as exc:
            if (
                revoke_on_failure
                and guardian_failure_reason is not None
                and self._guardian.armed
                and self._guardian.generation == generation
            ):
                self._revoke(guardian_failure_reason(exc))
            raise
        except Exception as exc:
            if revoke_on_failure and self._guardian.armed and self._guardian.generation == generation:
                self._revoke(failure_reason(exc))
            raise
        return self.run_after_begin(
            generation=generation,
            action=action,
            dispatch=dispatch,
            operation=operation,
            wait=wait,
            failure_reason=failure_reason,
            guardian_failure_reason=guardian_failure_reason,
            revoke_on_failure=revoke_on_failure,
            finish_when_current=finish_when_current,
            on_finish=on_finish,
            raise_finish_error=raise_finish_error,
        )

    def run_after_begin(
        self,
        *,
        generation: int,
        action: ActionCorrelation,
        dispatch: Callable[[], None],
        operation: Callable[[ActionCorrelation], _ResultT],
        wait: Callable[[ActionCorrelation], None],
        failure_reason: Callable[[Exception], str],
        guardian_failure_reason: Callable[[GuardianError], str] | None,
        revoke_on_failure: bool = False,
        finish_when_current: bool = False,
        on_finish: Callable[[bool, Exception | None], None] | None = None,
        raise_finish_error: bool = True,
    ) -> _ResultT:
        """Complete the lifecycle for an already-created Guardian action."""

        finish_succeeded = False
        finish_error: Exception | None = None

        def finish_unconditionally() -> None:
            nonlocal finish_succeeded, finish_error
            if finish_succeeded or finish_error is not None:
                return
            try:
                self._guardian.finish_action(generation, action)
                finish_succeeded = True
            except Exception as exc:
                finish_error = exc

        def finish_if_current() -> None:
            nonlocal finish_succeeded, finish_error
            if finish_succeeded or finish_error is not None:
                return
            if not self._guardian.armed or self._guardian.generation != generation:
                return
            try:
                self._guardian.finish_action(generation, action)
                finish_succeeded = True
            except Exception as exc:
                finish_error = exc

        try:
            dispatch()
            result = operation(action)
            wait(action)
        except GuardianError as exc:
            if (
                revoke_on_failure
                and guardian_failure_reason is not None
                and self._guardian.armed
                and self._guardian.generation == generation
            ):
                self._revoke(guardian_failure_reason(exc))
            raise
        except Exception as exc:
            if revoke_on_failure and self._guardian.armed and self._guardian.generation == generation:
                self._revoke(failure_reason(exc))
            raise
        else:
            if not finish_when_current:
                finish_unconditionally()
        finally:
            if finish_when_current:
                finish_if_current()
            if on_finish is not None:
                on_finish(finish_succeeded, finish_error)
            if finish_error is not None and raise_finish_error:
                raise finish_error
        return result


__all__ = ["ActionLifecycle", "GuardedActionRunner"]
