"""Offline demo of the full flow Fusion ERP (AR) -> OIC -> Cleo -> Customer.

Demonstrates, without any live system:
  1. Reading the Fusion AR source invoice and validating it (ERP-origin gaps).
  2. Validating a downstream OIC/EDI payload for the same invoice.
  3. Root-cause attribution: ERP_SOURCE vs OIC_MAPPING per missing field.

Writes a committed sample dataset for the Power BI dashboard.

    python src/demo.py

Reads data/sample_payloads/ only — touches no live OIC / Cleo / Fusion / SQL.
"""
from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

import config
import parsers
import rootcause
from models import (DefectOrigin, EXCEPTION_COLUMNS, InvoiceException,
                    NormalizedInvoice, Source, utcnow_iso)
from validator import validate

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "data/sample_payloads"


def main() -> int:
    logging.basicConfig(level="INFO", format="%(message)s")
    log = logging.getLogger("demo")
    required = config.load_required_fields()
    exceptions: list[InvoiceException] = []

    # --- Stage 1: Fusion ERP (system of origin) -> index + early detection ---
    erp_index = rootcause.ErpIndex()
    fusion_json = json.loads((SAMPLES / "fusion_ar_invoices.json").read_text())
    for rec in fusion_json["items"]:
        payload = parsers.parse_fusion_ar_invoice(rec)
        inv = NormalizedInvoice(
            source=Source.FUSION_ERP, integration_id=rec.get("BusinessUnit", "FUSION-AR"),
            integration_name="FUSION_AR_INVOICE", instance_id=str(rec.get("CustomerTransactionId", "")),
            document_id=payload["header"]["invoice_number"],
            customer_code=rec.get("BillToCustomerAccountNumber", ""),
            customer_name=rec.get("BillToCustomerName", ""),
            edi_standard="x12_810", document_type="810", transmission_status="ERP_SOURCE",
            payload=payload,
        )
        erp_index.add(inv.document_id, inv.payload)
        findings = validate(inv, config.resolve_schema(required, inv.edi_standard, inv.customer_code))
        for f in findings:
            f.defect_origin = DefectOrigin.ERP_SOURCE
        log.info("[FUSION] %s: %d ERP-origin gap(s): %s", inv.document_id, len(findings),
                 ", ".join(f.field_label for f in findings) or "none")
        if findings:
            exceptions.append(_exc(inv, findings))

    # --- Stage 2: downstream EDI payloads -> validate + attribute ------------
    downstream = {
        "invoice_x12_810_oic_dropped_fields.edi": "ACME-US",
        "invoice_x12_810_missing_fields.edi": "ACME-US",
        "invoice_edifact_invoic_complete.edi": "GLOBEX-EU",
    }
    for fname, customer in downstream.items():
        raw = (SAMPLES / fname).read_text(encoding="utf-8")
        std, payload = parsers.parse_raw(raw)
        inv = NormalizedInvoice(
            source=Source.OIC_API, integration_id=f"INT-{customer}",
            integration_name=f"INVOICE_OUT_{customer}", integration_version="01.00.0000",
            instance_id=fname, document_id=payload.get("header", {}).get("invoice_number") or fname,
            customer_code=customer, customer_name=customer.replace("-", " ").title(),
            edi_standard=std, document_type="810" if std == "x12_810" else "INVOIC",
            transmission_status="SENT", payload=payload,
        )
        findings = validate(inv, config.resolve_schema(required, std, customer))
        erp_payload = erp_index.get(inv.document_id)
        rootcause.attribute(findings, erp_payload)
        if findings:
            origins = ", ".join(f"{f.field_label}={f.defect_origin.value}" for f in findings)
            log.info("[OIC] %s (inv=%s, erp_match=%s): %s", fname, inv.document_id,
                     bool(erp_payload), origins)
            exceptions.append(_exc(inv, findings))
        else:
            log.info("[OIC] %s: clean", fname)

    out = ROOT / "data/sample_output/exceptions_sample.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=EXCEPTION_COLUMNS)
        w.writeheader()
        for exc in exceptions:
            w.writerow(exc.to_row())
    log.info("\nWrote %d exception(s) to %s", len(exceptions), out.relative_to(ROOT))
    return 0


def _exc(inv: NormalizedInvoice, findings) -> InvoiceException:
    return InvoiceException(
        run_id="demo", detected_at=utcnow_iso(), source=inv.source.value,
        integration_id=inv.integration_id, integration_name=inv.integration_name,
        integration_version=inv.integration_version, instance_id=inv.instance_id,
        document_id=inv.document_id, customer_code=inv.customer_code,
        customer_name=inv.customer_name, edi_standard=inv.edi_standard,
        document_type=inv.document_type, transmission_status=inv.transmission_status,
        findings=findings,
    )


if __name__ == "__main__":
    raise SystemExit(main())
