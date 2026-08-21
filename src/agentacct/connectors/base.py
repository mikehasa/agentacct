"""Deterministic JSON hashing primitives for the control-signal evaluator.

These helpers produce canonical JSON and a stable SHA-256 digest without
retaining the decoded input, so a control signal can be identified and
compared without exposing its contents.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping, Sequence


JsonScalar = str | int | float | bool | None
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class ConnectorError(ValueError):
    """Base class for bounded JSON parsing errors."""


def _json_safe(value: Any) -> JsonValue:
    """Return a canonical JSON-safe copy, rejecting non-finite numbers.

    The accepted surface is intentionally limited to ordinary decoded JSON so
    output never depends on ``repr`` of an arbitrary object.
    """

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ConnectorError("connector records cannot contain non-finite numbers")
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe(item) for item in value]
    raise ConnectorError(f"unsupported connector JSON value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Canonical JSON used for hashes, ordering, and replay identity."""

    return json.dumps(
        _json_safe(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def stable_digest(value: Any) -> str:
    """SHA-256 of decoded input without retaining the input itself."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
