# OIC Invoice Quality Agent

A read-only monitoring agent for the outbound customer-invoice flow that
**originates in Oracle Fusion Cloud Financials (AR)**, is integrated/translated by
**Oracle Integration Cloud (OIC)**, transmitted via **Cleo**, and delivered to
**customers** over **EDI**. It flags any invoice with **missing required fields**
at any stage, **attributes the root cause** (Fusion ERP data gap vs OIC mapping
loss), and publishes exceptions to a **Power BI dashboard** that flags the
affected integrations.

```
Fusion Cloud ERP (AR) ──▶ OIC ──▶ Cleo ──EDI──▶ Customer
        │                  │        │
        └────────┬─────────┴────────┘
                 ▼  (read-only)
   OIC Invoice Quality Agent
     • validate required fields at each stage
     • attribute defect_origin: ERP_SOURCE vs OIC_MAPPING
                 ▼
   exceptions (SQL + CSV) ──▶ Power BI dashboard
```

> **Guardrails first.** Read [`instructions.md`](instructions.md) — it defines
> everything the agent must, may, and must **not** do. The agent is strictly
> **observe-and-report**: it never modifies, resubmits, or repairs anything in
> OIC, Cleo, or the staging zone.

## What it does

1. **Polls** (on a cadence) four read-only sources — any/all enabled:
   - **Oracle Fusion Cloud Financials (AR) REST API** — `receivablesInvoices` (the **system of origin**)
   - OIC Monitoring/Audit REST API
   - File staging drop zone (where OIC writes before Cleo collects)
   - Cleo Integration Cloud / VLTrader / Harmony APIs
2. **Parses** Fusion AR invoices, X12 810, and EDIFACT INVOIC into one normalized shape.
3. **Validates** each invoice against the required-field schema in
   [`config/required_fields.yaml`](config/required_fields.yaml), including
   per-trading-partner overrides and conditional fields.
4. **Attributes root cause** by comparing the downstream payload to the Fusion
   source invoice: `ERP_SOURCE` (missing in Fusion AR) vs `OIC_MAPPING` (dropped
   in OIC). Catches ERP-origin gaps early ("shift left").
5. **Records exceptions** (presence/labels only — no raw values or PII) to
   **SQL** (`dbo.oic_invoice_exceptions`) and **CSV** (`data/exceptions.csv`).
6. **Publishes a heartbeat** for agent/source liveness.
7. Feeds the **Power BI dashboard** (see [`powerbi/`](powerbi/)).

## Layout

| Path | Purpose |
|------|---------|
| `instructions.md` | **Agent guardrails / operating manual** (authoritative). |
| `config/required_fields.yaml` | The only authority for mandatory fields (810 + INVOIC + partner overrides). |
| `config/settings.yaml` | Runtime settings (cadence, scope, sources, sinks, privacy). |
| `config/connections.example.env` | Credential template (copy to `.env`). |
| `src/agent.py` | Main monitoring loop (Fusion index → ERP early-detection → downstream + attribution). |
| `src/sources.py` | Read-only Fusion ERP / OIC / staging / Cleo readers. |
| `src/parsers.py` | Fusion AR + X12 810 + EDIFACT INVOIC → normalized payload. |
| `src/validator.py` | Missing-required-field detection. |
| `src/rootcause.py` | Defect-origin attribution (ERP_SOURCE vs OIC_MAPPING). |
| `src/exception_store.py` | CSV + SQL upsert sinks. |
| `powerbi/` | Dashboard spec, DAX measures, data dictionary. |
| `data/sample_payloads/` | Example invoices for the demo/tests. |
| `tests/` | Parser + validator tests. |

## Quick start

```bash
pip install -r requirements.txt

# 1. Configure credentials (read-only access) and sinks
cp config/connections.example.env .env   # then fill in / inject via secret store

# 2. Run the test suite
python -m pytest tests/ -q

# 3. Try it against the bundled sample invoices (no live systems needed)
python src/demo.py

# 4. Run a single live monitoring cycle
python src/agent.py --once

# 5. Run continuously on the configured cadence
python src/agent.py
```

## Power BI

Build the dashboard from [`powerbi/dashboard_spec.md`](powerbi/dashboard_spec.md)
against the `oic_invoice_exceptions` dataset (SQL DirectQuery or the CSV import),
add the measures from [`powerbi/measures.dax`](powerbi/measures.dax), and use the
heartbeat JSON for the liveness/source-health tiles. The dashboard flags every
integration with ≥1 open exception, a missing-field Pareto, per-customer
breakdown, 810-vs-INVOIC split, and a trend.

## Security & privacy

- Read-only against all sources; the `read_only` guardrail is enforced at startup
  and the HTTP layer refuses non-GET/HEAD methods.
- Stores **field labels and presence only** — never full payloads, line-item
  values, or customer PII.
- Credentials come from environment / secret store only; nothing sensitive is
  committed or logged.
