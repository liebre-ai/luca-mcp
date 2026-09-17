# IMSS tools for Luca (2026-09-17, reviewed by the ant-rai session)

Facts from ant-rai (its session answered on 2026-09-17; code under `src/rai/skills/sk_imss_idse/`,
contract `emision_schemas_v2.json`, notes `docs/imss_emision_v2.md`, plan
`docs/specs/2026-09-16-imss-idse-emision-PLAN.md`, merged in ant-rai PR #1067):

- One IMSS capability today: the IDSE **Emisión**. Per employer registration (registro
  patronal, 11 characters) and period, rai archives two cédulas de determinación de cuotas in
  Liebre's archive store (`source: "imss"`): `mx.emision_mensual_imss` (EMA, one calendar month:
  IMSS cuotas by branch) and `mx.emision_bimestral_imss` (EBA, one bimestre identified by its even
  closing month: RCV plus INFONAVIT). In odd months the EBA is not due. The IMSS issues the
  emisión of month M during the first days of M+1; payment is due on the 17th of M+1.
- `request_data` = `{registro_patronal, year, month}`. `extracted_data` (schema 2, closed):
  `patron` (registro, RFC, razón social, delegación, clase/prima RT, address), `emision` (tipo,
  year, month, period_label, bimestre, period_start/end, fecha_limite_pago, cotizantes,
  dias_cotizados, smv, uma, umi, propuestas), `conceptos` (per concept `{patronal, obrera, suma}`,
  MXN), `saldos` (`total`, `cuota_mensual`, `cuota_bimestral`), `asegurados[]` (per worker: NSS,
  CURP, name, suma, cotización rows with movement, days, salario base, per-concept amounts, and
  `sources` quoting the raw TXT lines), `extraction` (source, currency, sources, warnings).
  About 2 KB per worker: 70 to 90 KB for a 30-worker employer, a few MB for a thousand.
- Liebre's list endpoint returns no `extracted_data`: per row only the id, type, status,
  `requested_at`, `request_data`, `document_id`, `has_document`, `evidence_count` and
  `additional_document_count`. Amounts need the full archive.
- Files per archive: principal = the IDSE Excel workbook; additional document = the cédula PDF;
  evidences = SUA/Visor TXT files and screenshots. Only the newest archive of a registro and
  period holds the Documents slot (`document_id`).
- A business can hold several registros patronales. Reuse picks the newest completed archive of
  the same business, type, registro and period. rai writes IMSS archives as `completed` only.
- rai's generic MCP endpoints `GET .../sat_archives` and `.../sat_archives/{id|latest}` already
  reach IMSS archives (dev lists both types) but return `asegurados` whole and drop the PDF.
- No IMSS archive exists on dev yet: the producer needs an IMSS certificate configured through
  `IMSS_CREDENTIALS_JSON` on the rai service for a business whose RFC matches the employer's.
  Extractions start from the Luca chat ("emisión del IMSS de agosto 2026") or the jobs lane
  (`imss_get_emision_task`), never from `/api/v1/mcp`, which is read-only.

## Design

Two read-only tools in a new module `luca_mcp/tools/imss.py` (toolset `imss`, listed between
`sat` and `documents`). They mirror the SAT pair `list_declaraciones` (calendar) and
`get_sat_archive` (one record), with the IMSS vocabulary and a personal-data boundary built in.

### `list_imss_emisiones(business_id, year, registro_patronal=None, with_amounts=False, accounting_firm_id=None)`

Calendar of the archived IMSS emisiones of a year, per registro patronal, from the newest
completed archive of each period. No worker data. Two tiers:

- default: what Liebre's list gives, two list calls per calendar (status, ids, periods, dates);
- `with_amounts=true`: rai also reads the newest archive of every period (up to 18 full reads,
  bounded concurrency, short in-process cache keyed by archive id) and adds the amounts. The tool
  description says that amounts cost a full read per period.

```json
{
  "business_id": "bu-2", "year": 2026,
  "registros": [
    {
      "registro_patronal": "Z9999999901", "rfc": "AAA010101AAA", "razon_social": "EMPRESA DEMO SA DE CV",
      "months": [
        {"month": 8, "month_label": "agosto",
         "mensual": {"status": "archived", "sat_archive_id": "…", "document_id": "…",
                     "requested_at": "…", "archives_count": 2,
                     "period_start": "2026-08-01", "period_end": "2026-08-31",
                     "fecha_limite_pago": "2026-09-17", "cotizantes": 30, "dias_cotizados": 914,
                     "propuesta_imss": "…",
                     "importe_total": {"patronal": 138739.35, "obrera": 31281.69, "suma": 170021.04},
                     "saldos": {"total": 1008510.85, "cuota_mensual": 170021.04, "cuota_bimestral": 838489.81}},
         "bimestral": {"status": "archived", "bimestre": 4, "period_start": "2026-07-01",
                       "period_end": "2026-08-31", "sat_archive_id": "…", "document_id": "…",
                       "requested_at": "…", "archives_count": 1, "fecha_limite_pago": "…",
                       "cotizantes": 30, "creditos": 3, "propuesta_infonavit": "…",
                       "importe_total": {"patronal": …, "obrera": …, "suma": …},
                       "subtotal_rcv": {…}, "subtotal_infonavit": {…},
                       "saldos": {"total": …, "cuota_mensual": …, "cuota_bimestral": …}}},
        {"month": 9, "month_label": "septiembre",
         "mensual": {"status": "future"}, "bimestral": {"status": "not_due"}}
      ],
      "summary": {"months_archived": {"mensual": 8, "bimestral": 4},
                  "missing": {"mensual": [], "bimestral": []},
                  "archive_rows": 15,
                  "importe_total_suma": {"mensual": …, "bimestral": …}}
    }
  ],
  "summary": {"registros": 1, "months_archived": 12, "archive_rows": 15},
  "amounts_included": false,
  "platform_url": "https://app-dev.liebre.ai/…"
}
```

Amount fields (`importe_total`, `saldos`, `subtotal_*`, `cotizantes`, `dias_cotizados`,
`creditos`, `fecha_limite_pago`, `propuesta_*`) appear only with `with_amounts=true`.

Statuses per cédula: `archived`; `future` for every month at or after the current Mexico City
month (the emisión of a month only appears early the next month); `missing` for earlier months
without an archive; `not_due` (EBA in an odd month); or the newest archive's own status when it
is not `completed` (`processing`, `failed`, kept for safety). When the business has no IMSS
archive at all, `registros` is empty and `message` says that emisiones are extracted from the
Luca chat or the platform once the IMSS certificate is configured; the tool cannot start one.

Parameters: `year` (2000..2100), `registro_patronal` (10 or 11 characters as IDSE or the cédula
print them; separators dropped; a 10-character key matches by prefix; unknown value answers
`not_found` listing the registros that have archives), `with_amounts`, the usual `business_id`
and `accounting_firm_id`.

### `get_imss_emision(business_id, sat_archive_id=None, tipo=None, year=None, month=None, registro_patronal=None, include_workers=False, max_workers=50, accounting_firm_id=None)`

One cédula with its accounting content, selected by `sat_archive_id` or by `tipo`
(`mensual` | `bimestral`), `year`, `month` (the EBA's even closing month) and, when the business
has several, `registro_patronal`; the newest completed archive of that period wins.

Returned by default: identity (`sat_archive_id`, `tipo`, `registro_patronal`, `year`, `month`,
`bimestre`, `period_start`, `period_end`, `requested_at`, `status`, `schema_version`), `patron`,
`emision`, `conceptos`, `saldos`, `warnings` (from `extraction.warnings`), `files`
(`document` = the Excel workbook with `document_id`, name, type, size; `cedula_pdf` = the
additional document; `evidences_count`), `workers_total`, `platform_url`.

Personal data stays out unless asked: with `include_workers=true` the response adds `workers`
(at most `max_workers`, default 50, cap 500, about 400 bytes per row, `workers_truncated`), each
row `{nss, nombre, suma, credito: {numero, tipo_descuento, valor_descuento} | null,
cotizaciones: [{movimiento_code, movimiento_label, fecha, dias, salario_base_cotizacion, suma}]}`
(`movimiento_label` is null for most codes; the code is what to show). CURP, per-cotización
concept breakdowns and every `sources`/`raw` line are never returned by this tool. rai writes
one audit log line per worker read (business, archive, rows).

NSS policy, ant-rai's recommendation: full NSS, only under `include_workers=true` and capped,
because the NSS is the join key with payroll (the nómina CFDI carries it) and the same users
already open the workbook in Documents. The conservative alternative is the last four digits
with `nombre` unmasked. Decision for Anand.

File bytes are never embedded: `get_document(document_id)` mints the short-lived link for the
workbook, as for SAT documents; the PDF and TXT files are archive files.

### Not in this round

- A ledger-side counterpart (the IMSS/RCV/INFONAVIT cuotas booked in the period, to compare with
  the cédula the way `get_vat_determination` compares with the DIOT) needs an account mapping in
  rai first.
- Other IDSE modules on rai's roadmap (movimientos afiliatorios, confronta, DAPSUA, SATIC,
  dictamen, RTT) get their own rai paths under `/imss/...` and tools in the same module; an IMSS
  opinión de cumplimiento would be another archive type under `source=imss`.
- Triggering an extraction: the tools describe how it is started; they do not start it.

## What ant-rai provides (additive, one PR, on Anand's go)

1. `GET /api/v1/mcp/businesses/{id}/imss/emisiones?year=&registro_patronal=&with_amounts=`:
   two list calls (one per type, newest first, paged defensively), grouped by `request_data`
   (registro, year, month), newest completed per group; with amounts, the bounded detail reads.
2. `GET .../imss/emisiones/{sat_archive_id}` and
   `GET .../imss/emisiones/latest?tipo=&year=&month=&registro_patronal=` with `include_workers`
   and `max_workers`, shaped as above, additional document passed through.
3. Personal-data guard on the generic endpoint: for archives with `source == "imss"`,
   `get_sat_archive` returns `extracted_data` without `asegurados` and without
   `extraction.sources`, plus `workers_total` and a hint to use `get_imss_emision`.
4. Errors: `invalid_input` (year 2000..2100, month 1..12, `tipo` outside mensual|bimestral,
   `tipo=bimestral` with an odd month, registro format, `sat_archive_id` that is not an IMSS
   archive), `not_found` (no completed archive for the selection; unknown registro lists the
   known ones), plus the usual `unknown_business` and `forbidden`.
5. Registro normalisation reused from `get_emision.py` (`_registration_matches`): uppercase,
   separators dropped, exact for 11 characters, prefix for a 10-character key; the check digit is
   never invented.

## Client side (luca-mcp)

- `tools/imss.py` with the two tools; `MODULES` and the README toolsets gain `imss`;
  `_common.py` gains `DESC_REGISTRO_PATRONAL`; descriptions on every parameter.
- Prompts: vocabulary (IMSS, IDSE, registro patronal, EMA/EBA, cuota patronal/obrera, RCV,
  INFONAVIT, saldos, fecha límite de pago) in `luca_guidelines`; a "Review the IMSS cuotas of a
  period" workflow in `luca_navigation` (calendar, then the cédula, then the payment deadline;
  workers only on request; how to ask for a fresh extraction).
- README table rows; unit tests (21 tools, every parameter described); E2E cases against dev:
  calendar and cédula of the seeded bu-2 archives, `with_amounts`, `include_workers` cap,
  invalid inputs, `not_found`, and the guard on `get_sat_archive`.

## Test data: seed bu-2 on dev (ant-rai session, on Anand's go)

Feasible today with no Liebre change: `POST .../sat_archives` on dev validates `source=imss`,
the schema-2 request and extracted JSON (the payload built by rai's
`tests/rai/skills/sk_imss_idse/test_emision_detail.py` fixtures validates), the identity
cross-checks, an Excel principal (`.xlsx` from the openpyxl fixture) and a `%PDF-` additional
document. Side effects on bu-2/af-2: two archive rows, their files, and two Documents rows
(employer registration `Z9999999901`). Every file name tagged `LUCA-MCP-E2E`; the synthetic RFC
`AAA010101AAA` differs from bu-2's, so rai's reuse logic never picks the seed up. Seeded evidence
is never deleted, by convention.

## Evidence (2026-09-17, branch `feat/imss-tools`, version 0.4.0)

- Server side merged: ant-rai PR #1068 (https://github.com/liebre-ai/ant-rai/pull/1068), contract as
  agreed, 82 MCP tests, checked against dev Liebre with the bu-2 seed; dev deploy on merge.
- Seed on bu-2 (never delete): EMA August 2026 `ebf6312b-61b8-410d-adf7-d0ffa6c19b26` and EBA
  bimestre 4 `8912df36-50c1-4267-8ae0-2b2b2adbf2a3`, registro `Z9999999901`, synthetic RFC
  `AAA010101AAA`, files tagged `LUCA-MCP-E2E`.
- `ruff check` / `ruff format --check` clean; `uv run pytest` 11 passed (21 tools, every parameter
  described).
- E2E against the local rai serving the merged code: `tests/e2e/tools_suite.py --only imss` 2/2
  (calendar statuses, amounts tier, 10-character registro by prefix, `not_found` and
  `invalid_input` paths, worker cap with full NSS and no CURP/sources, the guard on
  `get_sat_archive`); the real `uv run luca-mcp` process returns the seeded cédula without worker
  rows. The full suite result is recorded in the PR.
- Dev verification after the deploy: from a logged-in client, `list_imss_emisiones(bu-2, 2026,
  with_amounts=true)` must show August archived with importe_total 1200 (EMA) and 5000 (EBA).
