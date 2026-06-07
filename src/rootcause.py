"""Root-cause attribution (instructions.md §4a).

Given the missing-field findings detected in a downstream payload (OIC / staging
/ Cleo) and the corresponding Fusion ERP **source** invoice, determine for each
finding whether the field:

  * was never populated in Fusion AR        -> ERP_SOURCE  (fix in the ERP / data entry)
  * existed in Fusion but was dropped in OIC -> OIC_MAPPING (fix in the integration mapping)

If no Fusion source invoice is available for the document, findings stay UNKNOWN.
This module is read-only and deterministic; it reuses the validator's resolver so
ERP and downstream payloads are compared with the same path vocabulary.
"""
from __future__ import annotations

from typing import Any

from models import DefectOrigin, Finding
from validator import _MISSING, _is_missing, _resolve


def _present_in_erp(erp_payload: dict[str, Any], path: str) -> bool:
    """True if the field at ``path`` is populated in the Fusion source invoice."""
    resolved = _resolve(erp_payload, path)
    if not resolved:
        return False
    # Present if at least one resolved node has a real value (lists: any line has it).
    return any((v is not _MISSING) and not _is_missing(v) for v in resolved)


def attribute(findings: list[Finding], erp_payload: dict[str, Any] | None) -> list[Finding]:
    """Annotate each finding with its DefectOrigin. Mutates and returns findings."""
    for f in findings:
        if erp_payload is None:
            f.defect_origin = DefectOrigin.UNKNOWN
        elif _present_in_erp(erp_payload, f.field_path):
            # Fusion had the value but it's missing downstream -> lost in OIC mapping.
            f.defect_origin = DefectOrigin.OIC_MAPPING
        else:
            # Missing at the ERP source too -> data-quality gap in Fusion AR.
            f.defect_origin = DefectOrigin.ERP_SOURCE
    return findings


class ErpIndex:
    """Lookup of Fusion source invoices keyed by invoice number for attribution."""

    def __init__(self) -> None:
        self._by_invoice: dict[str, dict[str, Any]] = {}

    def add(self, invoice_number: str, payload: dict[str, Any]) -> None:
        if invoice_number:
            self._by_invoice[str(invoice_number).strip()] = payload

    def get(self, invoice_number: str) -> dict[str, Any] | None:
        if not invoice_number:
            return None
        return self._by_invoice.get(str(invoice_number).strip())

    def __len__(self) -> int:
        return len(self._by_invoice)
