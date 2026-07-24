"""Small explicit registry for independently enabled read-only connectors."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .base import ConnectorError, ConnectorRecord, ReadOnlyConnector


class ConnectorRegistry:
    def __init__(self, connectors: Iterable[ReadOnlyConnector] = ()) -> None:
        self._connectors: dict[str, ReadOnlyConnector] = {}
        for connector in connectors:
            self.register(connector)

    def register(self, connector: ReadOnlyConnector) -> None:
        name = str(connector.name).strip()
        if not name:
            raise ConnectorError("connector name cannot be empty")
        if name in self._connectors:
            raise ConnectorError(f"connector already registered: {name}")
        self._connectors[name] = connector

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._connectors))

    def get(self, name: str) -> ReadOnlyConnector:
        try:
            return self._connectors[name]
        except KeyError as exc:
            raise ConnectorError(f"unknown connector: {name}") from exc

    def read(self, name: str, source: Any = None) -> tuple[ConnectorRecord, ...]:
        return self.get(name).read(source)


def build_default_registry() -> ConnectorRegistry:
    """Return snapshot/telemetry connectors that need no repository path."""

    from .openlit import OpenLITOTLPConnector
    from .paperclip import PaperclipSnapshotConnector

    return ConnectorRegistry((PaperclipSnapshotConnector(), OpenLITOTLPConnector()))
