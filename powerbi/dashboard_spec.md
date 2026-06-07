# Power BI Dashboard Spec — OIC Invoice Quality

This spec implements the dashboard requirements in `instructions.md §8`. Build it
in Power BI Desktop against the `oic_invoice_exceptions` dataset (see
`data_dictionary.md`) and the measures in `measures.dax`.

> **Flow monitored:** `Oracle Fusion Cloud ERP (AR) → OIC → Cleo → Customer (EDI)`.
> Each exception carries a `source` (which stage it was caught at) and a
> `defect_origin` (`ERP_SOURCE` vs `OIC_MAPPING`) so the dashboard can route the
> fix to the Fusion AR team or the OIC integration team.

## How to build (one-time)

1. **Get Data**
   - *Production:* SQL Server / Azure SQL → table `dbo.oic_invoice_exceptions` (DirectQuery).
   - *Portable / sample:* Text/CSV → `data/exceptions.csv` (Import).
   - Add `data/agent_heartbeat.json` (JSON connector) → `agent_heartbeat` table.
2. **Power Query transforms**
   - Set data types (`detected_at`, `first_seen_at`, `last_seen_at` → DateTime).
   - Duplicate the fact query → `exceptions_fields_unpivoted`: split `missing_fields`
     by `; ` (Text.Split) then *Expand to new rows*. This powers the field Pareto.
   - Create a `Calendar` date table; relate to `detected_at`.
3. **Add measures** from `measures.dax`.
4. **Build the pages below.**
5. **Schedule refresh** to match the agent cadence (or use DirectQuery for live).

---

## Page 1 — Executive Overview

| Zone | Visual | Field / Measure |
|------|--------|-----------------|
| Top KPI cards | Card | `[Open Exceptions]`, `[Integrations Affected]`, `[Customers Affected]`, `[Critical Exceptions]`, `[Pct Invoices With Missing Fields]` (%) |
| Liveness cards | Card | `[Agent Health]`, `[Agent Last Run]` |
| Left | Donut | Exceptions by `severity` (legend), value `[Open Exceptions]` |
| Center | **Clustered bar — Flagged Integrations** | Axis `integration_name`, value `[Open Exceptions]`, color by `severity`; sort desc. **This is the core "flag" view.** |
| Right | Bar | Exceptions by `customer_name` (top N = 10) |
| Mid row | **Donut — Defect Origin** | Legend `defect_origin`, value `[Open Exceptions]`; plus cards `[ERP Source Defects]`, `[OIC Mapping Defects]`, `[Pct Defects From ERP]`. Shows whether root cause is Fusion AR vs OIC mapping. |
| Bottom | Line | `[Total Exceptions]` by `Calendar[Date]` (trend) |

Add a **slicer** row: `edi_standard`, `severity`, `source`, `defect_origin`, `customer_name`, date range.

## Page 2 — Flagged Integrations (detail / drill-through)

- **Table** (the actionable list):
  `integration_name`, `integration_version`, `document_type`, `[Integration Flag]`,
  `[Open Exceptions]`, `[Total Missing Fields]`, `[Critical Exceptions]`,
  `[ERP Source Defects]`, `[OIC Mapping Defects]`, `[Owning Team]`,
  `[Avg Exception Age (days)]`, `[Stale Exceptions (>3d)]`.
  - Conditional formatting: data bars on `[Open Exceptions]`; color `[Integration Flag]`.
  - Sort: `severity` desc, then `[Open Exceptions]` desc.
- **Drill-through target** keyed on `integration_name` → exception-level table:
  `detected_at`, `instance_id`, `document_id`, `customer_name`, `missing_fields`,
  `severity`, `transmission_status`, `status`. (`instance_id` links analysts back
  to OIC/Cleo monitoring for the read-only source record.)

## Page 3 — Missing-Field Analysis

- **Pareto (bar + cumulative line)** on `exceptions_fields_unpivoted[missing_field]`,
  value = count — shows the top required fields that go missing.
- **Matrix:** rows `missing_field`, columns `customer_name`, values count — reveals
  partner-specific gaps (e.g., one customer always missing *Buyer VAT ID*).
- **100% stacked column:** `missing_field` share by `edi_standard` (810 vs INVOIC).
- **Stacked bar — field by origin:** `missing_field` (axis) by `defect_origin` (legend) — instantly shows which fields are an ERP data gap vs an OIC mapping loss.

## Page 4 — Operations / Health

- **Source health** matrix from `agent_heartbeat[source_health]` (Fusion ERP / OIC / staging / Cleo → OK/DEGRADED).
- **Cards:** `records_processed`, `exceptions_found`, `errors`, `[Agent Last Run]`, `config_hash`.
- **Trend:** exceptions/day split by `source`.
- **Alert banner:** a card bound to `agent_heartbeat[alert]` that turns red when a
  source has been degraded for ≥3 consecutive runs (instructions.md §9).

---

## Recommended interactions & alerts

- Enable **cross-filtering** so clicking an integration filters the field Pareto and customer bar.
- Configure a Power BI **data-driven alert** (or Power Automate) on `[Critical Exceptions] > 0`
  to notify the integration support channel — *informational only*; remediation stays human-driven
  (instructions.md §5.9).
- Pin Page-1 KPI cards to a shared **dashboard** for at-a-glance monitoring.

## Color / severity convention
`CRITICAL` = red `#D13438`, `HIGH` = orange `#F7630C`, `MEDIUM` = amber `#FFB900`,
`LOW` = grey `#8A8886`, `Clean` = green `#107C10`.
