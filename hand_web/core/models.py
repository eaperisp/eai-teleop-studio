"""Web-facing adapter contract for current and future dexterous hands."""

from __future__ import annotations

from typing import Any, Iterable, Protocol


class ValidationError(ValueError):
    pass


class HandAdapter(Protocol):
    @classmethod
    def capabilities(cls) -> dict[str, Any]: ...

    def connect(self, transport: str, options: dict[str, Any]) -> dict[str, Any]: ...

    def disconnect(self) -> dict[str, Any]: ...

    def status(self) -> dict[str, Any]: ...

    def command(
        self,
        side: str,
        positions: Iterable[int | float],
        duration_ms: int,
    ) -> dict[str, Any]: ...

    def stop(self) -> dict[str, Any]: ...
