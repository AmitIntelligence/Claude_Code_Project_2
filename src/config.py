"""Configuration loading + schema resolution.

Loads settings.yaml and required_fields.yaml, and resolves the effective
required-field schema for a given EDI standard + trading partner
(instructions.md §4.3, §4.6). Also exposes the config version hash for audit
logging (§10).
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _read_yaml(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_settings(path: str | Path = "config/settings.yaml") -> dict[str, Any]:
    return _read_yaml(ROOT / path)


def load_required_fields(path: str | Path = "config/required_fields.yaml") -> dict[str, Any]:
    return _read_yaml(ROOT / path)


def config_hash(*paths: str | Path) -> str:
    """Stable hash of the config files this run used (auditability, §10)."""
    h = hashlib.sha256()
    for p in paths:
        fp = ROOT / p
        if fp.exists():
            h.update(fp.read_bytes())
    return h.hexdigest()[:12]


def resolve_schema(
    required: dict[str, Any],
    edi_standard: str,
    customer_code: str = "",
) -> dict[str, Any]:
    """Build the effective field list: base standard + partner overrides.

    extra_required fields are appended; ``relax`` paths are removed.
    """
    base = required.get(edi_standard)
    if not base:
        raise KeyError(f"Unknown EDI standard in required_fields.yaml: {edi_standard!r}")

    fields = [dict(f) for f in base.get("fields", [])]

    overrides = (required.get("trading_partner_overrides") or {}).get(customer_code)
    if overrides:
        for extra in overrides.get("extra_required", []) or []:
            fields.append(dict(extra))
        relax = set(overrides.get("relax", []) or [])
        if relax:
            fields = [f for f in fields if f["path"] not in relax]

    return {"standard": edi_standard, "customer_code": customer_code, "fields": fields}


def env(name_or_ref: str, default: str = "") -> str:
    """Resolve an environment variable (credentials come from env only, §5.5)."""
    return os.environ.get(name_or_ref, default)
