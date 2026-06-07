"""Normalized data models shared across the agent.

Both X12 810 and EDIFACT INVOIC payloads are mapped into the same
``NormalizedInvoice`` shape so that ``config/required_fields.yaml`` can use a
single path vocabulary regardless of the source EDI standard.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"

    @property
    def rank(self) -> int:
        return {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}[self.value]


class Source(str, Enum):
    OIC_API = "OIC_API"
    STAGING_FILE = "STAGING_FILE"
    CLEO_API = "CLEO_API"


@dataclass
class NormalizedInvoice:
    """Source-agnostic view of one outbound invoice document.

    Parsers populate the subset of fields they can extract; missing fields are
    left as ``None``/empty so the validator can flag them. We deliberately keep
    only locating metadata + structural presence — never full raw values that
    could leak PII (instructions.md §5.4).
    """

    # Provenance / locating metadata
    source: Source
    integration_id: str
    integration_name: str
    integration_version: str = ""
    instance_id: str = ""
    document_id: str = ""
    customer_code: str = ""
    customer_name: str = ""
    edi_standard: str = ""          # "x12_810" | "edifact_invoic"
    document_type: str = ""         # "810" | "INVOIC"
    transmission_status: str = ""   # as reported by source (e.g. SENT, ERROR)

    # The normalized payload tree the validator walks. Nested dicts/lists keyed
    # to match required_fields.yaml paths (header.*, parties.*, lines[].*, ...).
    payload: dict[str, Any] = field(default_factory=dict)

    def locator(self) -> dict[str, str]:
        return {
            "integration_id": self.integration_id,
            "instance_id": self.instance_id,
            "document_id": self.document_id,
        }


@dataclass
class Finding:
    """A single missing required field within one document."""

    field_path: str
    field_label: str
    severity: Severity


@dataclass
class Exception_:
    """A grouped set of findings for one invoice document.

    Name has a trailing underscore to avoid shadowing the builtin; exported as
    ``InvoiceException`` below.
    """

    run_id: str
    detected_at: str
    source: str
    integration_id: str
    integration_name: str
    integration_version: str
    instance_id: str
    document_id: str
    customer_code: str
    customer_name: str
    edi_standard: str
    document_type: str
    findings: list[Finding]
    transmission_status: str = ""
    status: str = "OPEN"
    first_seen_at: str = ""
    last_seen_at: str = ""

    @property
    def exception_id(self) -> str:
        """Stable id for idempotent upserts (instructions.md §3.2)."""
        key = "|".join([
            self.integration_id,
            self.instance_id,
            self.document_id,
            ",".join(sorted(f.field_path for f in self.findings)),
        ])
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]

    @property
    def missing_fields(self) -> str:
        return "; ".join(f.field_label for f in self.findings)

    @property
    def missing_field_count(self) -> int:
        return len(self.findings)

    @property
    def severity(self) -> str:
        if not self.findings:
            return Severity.LOW.value
        return max((f.severity for f in self.findings), key=lambda s: s.rank).value

    def to_row(self) -> dict[str, Any]:
        """Flat record honoring the §6 allow-list (presence/labels only)."""
        return {
            "exception_id": self.exception_id,
            "run_id": self.run_id,
            "detected_at": self.detected_at,
            "source": self.source,
            "integration_id": self.integration_id,
            "integration_name": self.integration_name,
            "integration_version": self.integration_version,
            "instance_id": self.instance_id,
            "document_id": self.document_id,
            "customer_code": self.customer_code,
            "customer_name": self.customer_name,
            "edi_standard": self.edi_standard,
            "document_type": self.document_type,
            "missing_fields": self.missing_fields,
            "missing_field_count": self.missing_field_count,
            "severity": self.severity,
            "transmission_status": self.transmission_status,
            "status": self.status,
            "first_seen_at": self.first_seen_at or self.detected_at,
            "last_seen_at": self.last_seen_at or self.detected_at,
        }


# Public alias
InvoiceException = Exception_

# Ordered columns for CSV / SQL — the §6 allow-list, nothing more.
EXCEPTION_COLUMNS = [
    "exception_id", "run_id", "detected_at", "source",
    "integration_id", "integration_name", "integration_version",
    "instance_id", "document_id", "customer_code", "customer_name",
    "edi_standard", "document_type", "missing_fields", "missing_field_count",
    "severity", "transmission_status", "status", "first_seen_at", "last_seen_at",
]


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
