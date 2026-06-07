"""Tests for parsing + missing-field detection.

Run from repo root:  python -m pytest tests/  (or)  python tests/test_validator.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import config  # noqa: E402
import parsers  # noqa: E402
from validator import validate  # noqa: E402


REQUIRED = config.load_required_fields()


def _missing_labels(invoice_payload, edi_standard, customer=""):
    schema = config.resolve_schema(REQUIRED, edi_standard, customer)

    class _Inv:
        payload = invoice_payload
    return {f.field_label for f in validate(_Inv, schema)}


def test_x12_810_detects_missing_required_fields():
    raw = (ROOT / "data/sample_payloads/invoice_x12_810_missing_fields.edi").read_text()
    std, payload = parsers.parse_raw(raw)
    assert std == "x12_810"
    missing = _missing_labels(payload, std, customer="ACME-US")
    # The sample omits these mandatory fields:
    assert "Invoice Number" in missing
    assert "Ship-To Name" in missing
    assert "Total Invoice Amount" in missing
    assert "Line: Product ID" in missing
    assert "Currency Code" in missing
    # ACME-US partner override adds these:
    assert "Department Number" in missing
    assert "Ship-To DUNS" in missing
    # Present fields should NOT be flagged:
    assert "Bill-To Name" not in missing
    assert "Invoice Date" not in missing


def test_edifact_invoic_complete_has_no_missing_fields():
    raw = (ROOT / "data/sample_payloads/invoice_edifact_invoic_complete.edi").read_text()
    std, payload = parsers.parse_raw(raw)
    assert std == "edifact_invoic"
    # GLOBEX-EU override requires Buyer VAT ID (present) and relaxes Buyer GLN.
    missing = _missing_labels(payload, std, customer="GLOBEX-EU")
    assert missing == set(), f"unexpected missing fields: {missing}"


def test_empty_payload_flags_everything_failsafe():
    missing = _missing_labels({}, "x12_810")
    assert "Invoice Number" in missing
    assert "Bill-To Name" in missing


def test_conditional_tax_field():
    # Tax required only when indicator == Y for X12.
    payload_no_tax = {"tax": {"indicator": "N"}}
    assert "Tax Amount" not in _missing_labels(payload_no_tax, "x12_810")
    payload_tax_due = {"tax": {"indicator": "Y"}}  # amount missing
    assert "Tax Amount" in _missing_labels(payload_tax_due, "x12_810")


def test_whitespace_counts_as_missing():
    payload = {"header": {"invoice_number": "   "}}
    assert "Invoice Number" in _missing_labels(payload, "x12_810")


def test_severity_is_highest_among_findings():
    from models import Finding, InvoiceException, Severity
    exc = InvoiceException(
        run_id="r", detected_at="t", source="OIC_API", integration_id="i",
        integration_name="n", integration_version="", instance_id="", document_id="",
        customer_code="", customer_name="", edi_standard="x12_810", document_type="810",
        findings=[
            Finding("a", "A", Severity.LOW),
            Finding("b", "B", Severity.CRITICAL),
            Finding("c", "C", Severity.MEDIUM),
        ],
    )
    assert exc.severity == "CRITICAL"
    assert exc.missing_field_count == 3


def test_exception_id_is_stable_and_idempotent():
    from models import Finding, InvoiceException, Severity

    def make():
        return InvoiceException(
            run_id="r1", detected_at="t1", source="OIC_API", integration_id="INT1",
            integration_name="n", integration_version="", instance_id="INST1",
            document_id="DOC1", customer_code="", customer_name="",
            edi_standard="x12_810", document_type="810",
            findings=[Finding("header.invoice_number", "Invoice Number", Severity.CRITICAL)],
        )
    # Same locating data + same missing fields => same id across runs.
    assert make().exception_id == make().exception_id


def test_root_cause_attribution_erp_vs_oic():
    import json
    import parsers
    import rootcause
    from models import DefectOrigin

    # Build the ERP index from the Fusion AR sample.
    fusion = json.loads((ROOT / "data/sample_payloads/fusion_ar_invoices.json").read_text())
    idx = rootcause.ErpIndex()
    erp_payload = None
    for rec in fusion["items"]:
        p = parsers.parse_fusion_ar_invoice(rec)
        idx.add(p["header"]["invoice_number"], p)
        erp_payload = p
    assert len(idx) == 1
    # Fusion record HAS currency/total/product_id but is MISSING purchase order.
    assert erp_payload["currency_code"] == "USD"
    assert erp_payload["totals"]["invoice_amount"] in (62.5, "62.5", 62.50)

    # Downstream payload that dropped fields present in Fusion + lacks PO (also missing in Fusion).
    raw = (ROOT / "data/sample_payloads/invoice_x12_810_oic_dropped_fields.edi").read_text()
    std, payload = parsers.parse_raw(raw)

    class _Inv:
        pass
    inv = _Inv()
    inv.payload = payload
    schema = config.resolve_schema(REQUIRED, std, "ACME-US")
    findings = validate(inv, schema)
    erp = idx.get(payload["header"]["invoice_number"])
    assert erp is not None, "downstream invoice should match a Fusion source record"
    rootcause.attribute(findings, erp)

    origin = {f.field_label: f.defect_origin for f in findings}
    # Present in Fusion, dropped downstream => OIC_MAPPING
    assert origin["Total Invoice Amount"] == DefectOrigin.OIC_MAPPING
    assert origin["Currency Code"] == DefectOrigin.OIC_MAPPING
    assert origin["Line: Product ID"] == DefectOrigin.OIC_MAPPING
    assert origin["Ship-To Name"] == DefectOrigin.OIC_MAPPING
    # Missing in Fusion too => ERP_SOURCE
    assert origin["Purchase Order Number"] == DefectOrigin.ERP_SOURCE
    assert origin["Department Number"] == DefectOrigin.ERP_SOURCE


def test_attribution_unknown_without_erp_source():
    import rootcause
    from models import DefectOrigin, Finding, Severity
    findings = [Finding("header.invoice_number", "Invoice Number", Severity.CRITICAL)]
    rootcause.attribute(findings, None)
    assert findings[0].defect_origin == DefectOrigin.UNKNOWN


def test_exception_defect_origin_is_mixed():
    from models import DefectOrigin, Finding, InvoiceException, Severity
    exc = InvoiceException(
        run_id="r", detected_at="t", source="OIC_API", integration_id="i",
        integration_name="n", integration_version="", instance_id="", document_id="",
        customer_code="", customer_name="", edi_standard="x12_810", document_type="810",
        findings=[
            Finding("a", "A", Severity.HIGH, DefectOrigin.ERP_SOURCE),
            Finding("b", "B", Severity.HIGH, DefectOrigin.OIC_MAPPING),
        ],
    )
    assert exc.defect_origin == "MIXED"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    print(f"\n{'OK' if failures == 0 else f'{failures} FAILED'}")
    sys.exit(1 if failures else 0)
