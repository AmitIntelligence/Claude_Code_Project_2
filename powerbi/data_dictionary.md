# Power BI Data Dictionary — `oic_invoice_exceptions`

This is the **only** dataset the dashboard consumes. It is produced by the agent
in two interchangeable forms (instructions.md §7):

- **SQL:** table `dbo.oic_invoice_exceptions` (DirectQuery or scheduled refresh)
- **CSV:** `data/exceptions.csv` (Import)

Plus a small companion source for agent liveness:

- **Heartbeat:** `data/agent_heartbeat.json` (Import) → drives the *Source health* / *Agent last run* tiles.

> Per the §6 allow-list, this dataset stores **field names/labels and presence only** —
> never raw invoice values, monetary line detail, or customer PII.

## Columns

| Column | Type | Description |
|--------|------|-------------|
| `exception_id` | text (PK) | Stable hash of integration+instance+document+missing-field set. Used for idempotent upsert. |
| `run_id` | text | The monitoring run that last detected this exception. |
| `detected_at` | datetime | When this exception was last detected (UTC). |
| `source` | text | `FUSION_ERP` / `OIC_API` / `STAGING_FILE` / `CLEO_API` — which stage the payload was read at. |
| `integration_id` | text | OIC integration identifier. |
| `integration_name` | text | OIC integration name (the thing the dashboard *flags*). |
| `integration_version` | text | Integration version. |
| `instance_id` | text | OIC instance / Cleo transfer id (drill-through key to source monitoring). |
| `document_id` | text | Invoice/document business key. |
| `customer_code` | text | Trading-partner code. |
| `customer_name` | text | Trading-partner display name. |
| `edi_standard` | text | `x12_810` or `edifact_invoic`. |
| `document_type` | text | `810` or `INVOIC`. |
| `missing_fields` | text | `;`-separated list of the **labels** of missing required fields. |
| `missing_field_count` | int | Number of missing fields in this document. |
| `severity` | text | `CRITICAL` / `HIGH` / `MEDIUM` / `LOW` — highest among the findings. |
| `defect_origin` | text | Root cause: `ERP_SOURCE` (missing in Fusion AR) / `OIC_MAPPING` (dropped in OIC) / `MIXED` / `UNKNOWN`. Routes the fix to the right team. |
| `transmission_status` | text | Status as reported by the source (e.g. `ERROR`, `SENT`, `STAGED`). |
| `status` | text | Triage state: `OPEN` (default) / `ACK` / `RESOLVED`. Preserved across runs. |
| `first_seen_at` | datetime | First detection (for ageing/trend). |
| `last_seen_at` | datetime | Most recent detection. |

## Suggested Power BI modeling

- Mark `oic_invoice_exceptions` as the fact table.
- Create a **Date** table (`Calendar`) related to `detected_at` (or `first_seen_at`) for the trend visuals.
- Build a small **Severity** lookup ( CRITICAL=4 … LOW=1 ) for correct sort order.
- For the *missing-field Pareto*, split `missing_fields` with **Text.Split** in Power Query to get one row per missing field, into a companion query `exceptions_fields_unpivoted`.

## Refresh

- **DirectQuery (SQL):** near-real-time; the dashboard reflects each agent run immediately.
- **Import (CSV/SQL):** configure a Power BI scheduled refresh aligned to the agent's `poll_interval_seconds` (default 5 min → use 8× daily or Premium more-frequent refresh).
