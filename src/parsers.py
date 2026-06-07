"""Parse raw invoice payloads into the NormalizedInvoice payload tree.

Supports the two configured standards:
  * ANSI X12 810  (segment/element delimited, e.g. ``BIG*...~``)
  * UN/EDIFACT INVOIC (e.g. ``BGM+...'``)

These parsers extract only the fields referenced by required_fields.yaml. They
do NOT retain full raw values beyond what is needed to test presence; the agent
stores presence/absence only downstream (instructions.md §5.4).

If the agent receives an already-structured payload (JSON/XML mapped by OIC),
``parse_structured`` passes it through.
"""
from __future__ import annotations

from typing import Any


def detect_standard(raw: str) -> str:
    """Best-effort detection of the EDI standard from raw text."""
    head = raw.lstrip()[:512].upper()
    if head.startswith("ISA") or "BIG*" in head or "\nST*810" in head or "ST*810" in head:
        return "x12_810"
    if head.startswith("UNA") or head.startswith("UNB") or "BGM+" in head or "INVOIC" in head:
        return "edifact_invoic"
    return ""


# ---------------------------------------------------------------------------
# X12 810
# ---------------------------------------------------------------------------
def parse_x12_810(raw: str, elem: str = "*", seg: str = "~") -> dict[str, Any]:
    """Map an X12 810 string into the normalized payload tree."""
    payload: dict[str, Any] = {
        "header": {}, "parties": {}, "totals": {}, "tax": {}, "lines": [],
    }
    n1_role_map = {"RI": "remit_to", "BT": "bill_to", "ST": "ship_to", "BY": "buyer"}

    segments = [s.strip() for s in raw.replace("\n", "").split(seg) if s.strip()]
    current_party: str | None = None

    for s in segments:
        e = s.split(elem)
        tag = e[0].strip().upper()

        def g(i: int) -> Any:
            return e[i].strip() if i < len(e) and e[i].strip() != "" else None

        if tag == "BIG":
            payload["header"]["invoice_date"] = g(1)
            payload["header"]["invoice_number"] = g(2)
            payload["header"]["purchase_order_number"] = g(4)
        elif tag == "CUR":
            payload["currency_code"] = g(2)
        elif tag == "N1":
            role = n1_role_map.get((g(1) or "").upper())
            current_party = role
            if role:
                payload["parties"].setdefault(role, {})
                payload["parties"][role]["name"] = g(2)
                payload["parties"][role]["id_code"] = g(4)
        elif tag == "REF" and current_party == "ship_to" and (g(1) or "").upper() == "01":
            payload["parties"]["ship_to"]["id_code"] = g(2)
        elif tag == "DTM":
            pass  # dates captured via BIG for 810 baseline
        elif tag == "TXI":
            payload["tax"]["indicator"] = "Y"
            payload["tax"]["amount"] = g(2)
        elif tag == "IT1":
            payload["lines"].append({
                "quantity": g(2),
                "uom": g(3),
                "unit_price": g(4),
                "product_id": g(7),
            })
        elif tag == "TDS":
            payload["totals"]["invoice_amount"] = g(1)
        elif tag == "CTT":
            payload["totals"]["line_item_count"] = g(1)
        elif tag == "REF" and (g(1) or "").upper() == "DP":
            payload["header"]["department_number"] = g(2)

    return payload


# ---------------------------------------------------------------------------
# EDIFACT INVOIC
# ---------------------------------------------------------------------------
def parse_edifact_invoic(raw: str) -> dict[str, Any]:
    """Map a UN/EDIFACT INVOIC string into the normalized payload tree."""
    payload: dict[str, Any] = {
        "header": {}, "parties": {}, "totals": {}, "tax": {}, "lines": [],
    }
    # Honor UNA if present, else defaults.
    comp, elem, seg = ":", "+", "'"
    text = raw.strip()
    if text.upper().startswith("UNA") and len(text) >= 9:
        comp, elem = text[3], text[4]
        seg = text[8]
        text = text[9:]

    nad_map = {"SU": "supplier", "BY": "buyer", "DP": "ship_to", "IV": "bill_to"}
    segments = [s.strip() for s in text.replace("\n", "").split(seg) if s.strip()]
    current_party: str | None = None
    in_line = False

    for s in segments:
        e = s.split(elem)
        tag = e[0].strip().upper()

        def comp_at(idx: int, cidx: int = 0) -> Any:
            if idx >= len(e):
                return None
            parts = e[idx].split(comp)
            v = parts[cidx].strip() if cidx < len(parts) else ""
            return v or None

        if tag == "BGM":
            payload["header"]["invoice_number"] = comp_at(2)
        elif tag == "DTM":
            # DTM+137:date:format -> invoice date
            if (comp_at(1) or "") == "137":
                payload["header"]["invoice_date"] = comp_at(1, 1)
        elif tag == "RFF":
            if (comp_at(1) or "").upper() == "ON":
                payload["header"]["purchase_order_number"] = comp_at(1, 1)
            elif (comp_at(1) or "").upper() == "VA":
                if current_party:
                    payload["parties"].setdefault(current_party, {})["vat_id"] = comp_at(1, 1)
        elif tag == "NAD":
            role = nad_map.get((comp_at(1) or "").upper())
            current_party = role
            if role:
                payload["parties"].setdefault(role, {})
                payload["parties"][role]["gln"] = comp_at(2)
                # NAD name components live in element 4 (C080)
                payload["parties"][role]["name"] = comp_at(4) or comp_at(2)
        elif tag == "CUX":
            payload["currency_code"] = comp_at(1, 1)
        elif tag == "LIN":
            in_line = True
            payload["lines"].append({"product_id": comp_at(3)})
        elif tag == "QTY" and in_line and payload["lines"]:
            payload["lines"][-1]["quantity"] = comp_at(1, 1)
        elif tag == "PRI" and in_line and payload["lines"]:
            payload["lines"][-1]["unit_price"] = comp_at(1, 1)
        elif tag == "MOA":
            code = comp_at(1) or ""
            amount = comp_at(1, 1)
            if in_line and payload["lines"] and code == "203":
                payload["lines"][-1]["line_amount"] = amount
            elif code == "77":
                payload["totals"]["invoice_amount"] = amount
            elif code == "125":
                payload["totals"]["taxable_amount"] = amount
            elif code == "124":
                payload["tax"]["amount"] = amount
                payload["tax"].setdefault("indicator", "S")
        elif tag == "TAX":
            cat = (comp_at(6) or comp_at(1) or "").upper()
            payload["tax"]["indicator"] = "E" if cat == "E" else payload["tax"].get("indicator", "S")

    return payload


def parse_structured(payload: dict[str, Any]) -> dict[str, Any]:
    """Pass-through for payloads already mapped to the normalized shape."""
    return payload or {}


# ---------------------------------------------------------------------------
# Oracle Fusion Cloud Financials — Receivables (AR) invoice
# Maps the receivablesInvoices REST resource (+ child lines) into the SAME
# normalized payload tree, so root-cause attribution can compare the ERP source
# against the downstream payload using one path vocabulary (instructions.md §4a).
# ---------------------------------------------------------------------------
def parse_fusion_ar_invoice(rec: dict[str, Any]) -> dict[str, Any]:
    """Map a Fusion AR receivablesInvoices record to the normalized shape.

    Field name variants from common Fusion REST/BIP/BICC shapes are tolerated.
    In Fusion AR the deploying company is the supplier and the customer is the
    bill-to/buyer, so both X12 and EDIFACT party paths are populated.
    """
    def pick(*names: str) -> Any:
        for n in names:
            if n in rec and str(rec.get(n)).strip() not in ("", "None"):
                return rec.get(n)
        return None

    cust_name = pick("BillToCustomerName", "BillToCustomer", "CustomerName")
    cust_acct = pick("BillToCustomerAccountNumber", "BillToCustomerNumber", "CustomerAccountNumber")
    ship_name = pick("ShipToCustomerName", "ShipToCustomer")

    payload: dict[str, Any] = {
        "header": {
            "invoice_number": pick("TransactionNumber", "InvoiceNumber", "DocumentNumber"),
            "invoice_date": pick("TransactionDate", "InvoiceDate"),
            "purchase_order_number": pick("PurchaseOrder", "CustomerPoNumber", "PurchaseOrderNumber"),
            "department_number": pick("DepartmentNumber", "Department"),
        },
        "parties": {
            "bill_to": {"name": cust_name, "id_code": cust_acct},
            "buyer": {
                "name": cust_name,
                "gln": pick("BillToCustomerGln", "CustomerGln"),
                "vat_id": pick("BillToCustomerTaxRegistrationNumber", "CustomerTaxRegistrationNumber"),
            },
            "ship_to": {"name": ship_name, "id_code": pick("ShipToCustomerSiteNumber", "ShipToSiteNumber")},
            "remit_to": {"name": pick("RemitToName", "BusinessUnit")},
            "supplier": {
                "name": pick("BusinessUnit", "LegalEntity", "RemitToName"),
                "vat_id": pick("LegalEntityTaxRegistrationNumber", "FirstPartyTaxRegistrationNumber"),
            },
        },
        "currency_code": pick("InvoiceCurrencyCode", "CurrencyCode"),
        "totals": {
            "invoice_amount": pick("EnteredAmount", "InvoiceAmount", "TotalAmount"),
            "taxable_amount": pick("TaxableAmount", "EnteredTaxableAmount"),
            "line_item_count": pick("LineCount", "NumberOfLines"),
        },
        "tax": {
            "amount": pick("TaxAmount", "EnteredTaxAmount"),
            "indicator": pick("TaxClassificationCode", "TaxStatus"),
        },
        "lines": [],
    }

    raw_lines = rec.get("receivablesInvoiceLines") or rec.get("lines") or rec.get("InvoiceLines") or []
    if isinstance(raw_lines, dict):  # REST sometimes wraps children in {"items": [...]}
        raw_lines = raw_lines.get("items", [])
    for ln in raw_lines:
        def lpick(*names: str) -> Any:
            for n in names:
                if n in ln and str(ln.get(n)).strip() not in ("", "None"):
                    return ln.get(n)
            return None
        payload["lines"].append({
            "product_id": lpick("InventoryItemNumber", "ItemNumber", "MemoLineName", "Description"),
            "quantity": lpick("Quantity", "InvoicedQuantity"),
            "unit_price": lpick("UnitSellingPrice", "UnitPrice"),
            "uom": lpick("UnitOfMeasureCode", "UOMCode", "UnitOfMeasure"),
            "line_amount": lpick("LineAmount", "ExtendedAmount", "Amount"),
        })
    if payload["totals"]["line_item_count"] is None and payload["lines"]:
        payload["totals"]["line_item_count"] = len(payload["lines"])
    return payload


def parse_raw(raw: str) -> tuple[str, dict[str, Any]]:
    """Detect standard and parse. Returns (edi_standard, payload)."""
    std = detect_standard(raw)
    if std == "x12_810":
        return std, parse_x12_810(raw)
    if std == "edifact_invoic":
        return std, parse_edifact_invoic(raw)
    return "", {}
