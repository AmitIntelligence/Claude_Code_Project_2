"""Missing-required-field detection (instructions.md §4).

The validator is the heart of the agent. It is pure and deterministic: given a
``NormalizedInvoice`` and the loaded field schema, it always returns the same
findings. It never mutates the payload and never stores raw values.
"""
from __future__ import annotations

from typing import Any

from models import Finding, NormalizedInvoice, Severity


def _is_missing(value: Any) -> bool:
    """A field is missing if absent, null, empty, or whitespace-only (§4.1)."""
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    return False


def _resolve(payload: dict[str, Any], path: str) -> list[Any]:
    """Resolve a required_fields.yaml path against the normalized payload.

    Supports dotted paths and a single ``[]`` list segment, e.g.
    ``lines[].product_id`` -> the product_id of every line. Returns a list of
    resolved values (one per matched node); an empty list means the container
    itself was absent, which counts as missing.
    """
    parts = path.split(".")
    current: list[Any] = [payload]

    for part in parts:
        nxt: list[Any] = []
        list_segment = part.endswith("[]")
        key = part[:-2] if list_segment else part

        for node in current:
            if not isinstance(node, dict):
                continue
            val = node.get(key, _MISSING)
            if list_segment:
                if isinstance(val, list):
                    nxt.extend(val if val else [_MISSING])  # empty list => missing
                else:
                    nxt.append(_MISSING)
            else:
                nxt.append(val)
        current = nxt

    return current


_MISSING = object()


def _condition_met(payload: dict[str, Any], condition: str | None) -> bool:
    """Evaluate a simple '<path> == <value>' / '<path> != <value>' condition."""
    if not condition:
        return True
    for op in ("==", "!="):
        if op in condition:
            left, right = (s.strip() for s in condition.split(op, 1))
            values = _resolve(payload, left)
            actual = values[0] if values else None
            actual = "" if actual is _MISSING or actual is None else str(actual).strip()
            return (actual == right) if op == "==" else (actual != right)
    # Unknown condition syntax -> treat field as required (fail safe).
    return True


def validate(invoice: NormalizedInvoice, schema: dict[str, Any]) -> list[Finding]:
    """Return the list of missing-field findings for one invoice.

    ``schema`` is the resolved field list for this invoice's standard +
    trading-partner overrides (produced by config.resolve_schema).
    """
    findings: list[Finding] = []

    for spec in schema["fields"]:
        path = spec["path"]
        condition = spec.get("condition")
        if not _condition_met(invoice.payload, condition):
            continue

        resolved = _resolve(invoice.payload, path)
        # No resolved nodes -> the field/container is absent -> missing.
        missing = (not resolved) or any(
            (v is _MISSING) or _is_missing(v) for v in resolved
        )
        if missing:
            findings.append(
                Finding(
                    field_path=path,
                    field_label=spec.get("label", path),
                    severity=Severity(spec.get("severity", "MEDIUM")),
                )
            )

    return findings
