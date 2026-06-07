"""Offline demo: run the validator over the bundled sample invoices and write
exceptions to a committed sample output, so the Power BI dashboard has data to
bind to without any live OIC/Cleo/SQL connection.

    python src/demo.py

This does NOT touch any live system — it reads data/sample_payloads/ only.
"""
from __future__ import annotations

import logging
from pathlib import Path

import config
import parsers
from models import InvoiceException, Source, NormalizedInvoice, utcnow_iso
from validator import validate

ROOT = Path(__file__).resolve().parent.parent

# Map each sample file to the trading partner it represents.
SAMPLES = {
    "invoice_x12_810_missing_fields.edi": "ACME-US",
    "invoice_edifact_invoic_complete.edi": "GLOBEX-EU",
}


def main() -> int:
    logging.basicConfig(level="INFO", format="%(message)s")
    log = logging.getLogger("demo")
    required = config.load_required_fields()

    exceptions: list[InvoiceException] = []
    sample_dir = ROOT / "data/sample_payloads"
    for fname, customer in SAMPLES.items():
        raw = (sample_dir / fname).read_text(encoding="utf-8")
        std, payload = parsers.parse_raw(raw)
        inv = NormalizedInvoice(
            source=Source.STAGING_FILE,
            integration_id=f"INT-{fname.split('_')[1].upper()}",
            integration_name=f"INVOICE_OUT_{customer}",
            integration_version="01.00.0000",
            instance_id=fname,
            document_id=payload.get("header", {}).get("invoice_number") or fname,
            customer_code=customer,
            customer_name=customer.replace("-", " ").title(),
            edi_standard=std,
            document_type="810" if std == "x12_810" else "INVOIC",
            transmission_status="STAGED",
            payload=payload,
        )
        schema = config.resolve_schema(required, std, customer)
        findings = validate(inv, schema)
        log.info("%s (%s): %d missing field(s): %s", fname, std, len(findings),
                 ", ".join(f.field_label for f in findings) or "none")
        if findings:
            exceptions.append(InvoiceException(
                run_id="demo", detected_at=utcnow_iso(), source=inv.source.value,
                integration_id=inv.integration_id, integration_name=inv.integration_name,
                integration_version=inv.integration_version, instance_id=inv.instance_id,
                document_id=inv.document_id, customer_code=inv.customer_code,
                customer_name=inv.customer_name, edi_standard=inv.edi_standard,
                document_type=inv.document_type, transmission_status=inv.transmission_status,
                findings=findings,
            ))

    # Write a committed sample output for the dashboard.
    import csv
    from models import EXCEPTION_COLUMNS
    out = ROOT / "data/sample_output/exceptions_sample.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=EXCEPTION_COLUMNS)
        w.writeheader()
        for exc in exceptions:
            w.writerow(exc.to_row())
    log.info("\nWrote %d exception(s) to %s", len(exceptions), out.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
