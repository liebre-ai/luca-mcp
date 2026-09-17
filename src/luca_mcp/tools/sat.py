"""The SAT side of a business: monthly filings, SAT archives and the Article 69-B check."""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from luca_mcp.api import RaiClient
from luca_mcp.tools._common import (
    DESC_END_PERIOD_ID,
    DESC_START_PERIOD_ID,
    DESC_YEAR,
    READ_ONLY,
    AccountingFirmId,
    BusinessId,
    Limit,
    Offset,
    segment,
)

DESC_EXTRACTION_TYPE = (
    "Archive type to filter by, for example `mx.declaracion_mensual_diot` or "
    "`mx.constancia_de_situacion_fiscal`. Call `list_sat_archives` once and check the result's "
    "`available_types` for the exact values available to this business; omit to return archives "
    "of every type."
)
DESC_STATUS = (
    "Extraction status to filter by: `processing`, `completed` or `failed`. Omit to return "
    "archives in any status."
)
DESC_REQUESTED_FROM = "Only include archives requested on or after this date, as `YYYY-MM-DD`."
DESC_REQUESTED_TO = "Only include archives requested on or before this date, as `YYYY-MM-DD`."
DESC_SAT_ARCHIVE_ID = (
    "Archive id exactly as returned by `list_sat_archives`; never guess it. Omit to select by "
    "`extraction_type` instead."
)
DESC_EXTRACTION_TYPE_LATEST = (
    "Archive type such as `mx.declaracion_mensual_diot`, used only when `sat_archive_id` is "
    "omitted to return the latest completed archive of this type."
)
DESC_INCLUDE_EXTRACTED_DATA = (
    "Whether to include the archive's structured `extracted_data` (what was declared to the "
    "SAT). Set to `false` to return metadata and files only, without the declared figures."
)
DESC_MAX_DETAIL_ROWS = (
    "Cap on the number of rows returned in `details` (for example one row per supplier for a "
    "DIOT archive). Default 100."
)
DESC_STATUSES_69B = (
    "Article 69-B statuses to include: `presunto`, `definitivo`, `desvirtuado` or "
    "`sentencia_favorable`. Defaults to the risk-bearing statuses, `presunto` and `definitivo`; "
    "add `desvirtuado` and/or `sentencia_favorable` to also include taxpayers later cleared."
)


def register(mcp: FastMCP, client: RaiClient) -> None:
    @mcp.tool(name="list_declaraciones", annotations=READ_ONLY)
    async def list_declaraciones(
        business_id: BusinessId,
        year: Annotated[int, Field(description=DESC_YEAR)],
        accounting_firm_id: AccountingFirmId = None,
    ) -> Any:
        """Monthly SAT tax filings (declaraciones) of a business for a year: per month the deadline
        and, per declaration type, the status (presented, overdue, upcoming, future,
        not_applicable), the primary filing when known (date, folio, amount paid, acuse link that
        expires within minutes) and the number of complementary filings."""
        return await client.fetch(
            f"/businesses/{segment(business_id, 'business_id')}/declaraciones",
            year=year,
            accounting_firm_id=accounting_firm_id,
        )

    @mcp.tool(name="list_sat_archives", annotations=READ_ONLY)
    async def list_sat_archives(
        business_id: BusinessId,
        extraction_type: Annotated[str | None, Field(description=DESC_EXTRACTION_TYPE)] = None,
        status: Annotated[str | None, Field(description=DESC_STATUS)] = None,
        requested_from: Annotated[str | None, Field(description=DESC_REQUESTED_FROM)] = None,
        requested_to: Annotated[str | None, Field(description=DESC_REQUESTED_TO)] = None,
        limit: Limit = 50,
        offset: Offset = 0,
        accounting_firm_id: AccountingFirmId = None,
    ) -> Any:
        """List what Luca extracted from the SAT portal for a business (SAT archives), newest first:
        type (e.g. `mx.declaracion_mensual_diot`, `mx.constancia_de_situacion_fiscal`), request
        (year/month), status (processing, completed, failed), document and evidence counts.
        `available_types` lists valid `extraction_type` values. Dates are YYYY-MM-DD."""
        return await client.fetch(
            f"/businesses/{segment(business_id, 'business_id')}/sat_archives",
            extraction_type=extraction_type,
            status=status,
            requested_from=requested_from,
            requested_to=requested_to,
            limit=limit,
            offset=offset,
            accounting_firm_id=accounting_firm_id,
        )

    @mcp.tool(
        name="get_sat_archive",
        annotations=READ_ONLY,
        meta={"anthropic/maxResultSizeChars": 300_000},
    )
    async def get_sat_archive(
        business_id: BusinessId,
        sat_archive_id: Annotated[str | None, Field(description=DESC_SAT_ARCHIVE_ID)] = None,
        extraction_type: Annotated[
            str | None, Field(description=DESC_EXTRACTION_TYPE_LATEST)
        ] = None,
        include_extracted_data: Annotated[
            bool, Field(description=DESC_INCLUDE_EXTRACTED_DATA)
        ] = True,
        max_detail_rows: Annotated[int, Field(description=DESC_MAX_DETAIL_ROWS)] = 100,
        accounting_firm_id: AccountingFirmId = None,
    ) -> Any:
        """One SAT archive with its structured `extracted_data` (what was declared to the SAT). Pass
        `sat_archive_id` or, instead, `extraction_type` for the latest completed archive of that
        type. DIOT archives carry `filing`, `totals` (declared VAT figures) and `details` (one row
        per supplier, capped at `max_detail_rows`). `include_extracted_data=false` returns metadata
        and files only."""
        return await client.fetch(
            f"/businesses/{segment(business_id, 'business_id')}/sat_archives/{segment(sat_archive_id, 'sat_archive_id') if sat_archive_id else 'latest'}",
            extraction_type=extraction_type,
            include_extracted_data=include_extracted_data,
            max_detail_rows=max_detail_rows,
            accounting_firm_id=accounting_firm_id,
        )

    @mcp.tool(name="list_article_69b_matches", annotations=READ_ONLY)
    async def list_article_69b_matches(
        business_id: BusinessId,
        start_period_id: Annotated[str, Field(description=DESC_START_PERIOD_ID)],
        end_period_id: Annotated[str | None, Field(description=DESC_END_PERIOD_ID)] = None,
        statuses: Annotated[list[str] | None, Field(description=DESC_STATUSES_69B)] = None,
        limit: Limit = 50,
        offset: Offset = 0,
        accounting_firm_id: AccountingFirmId = None,
    ) -> Any:
        """Customers and suppliers of the business that appear in the SAT's Article 69-B list
        (EFOS: taxpayers presumed or confirmed to invoice simulated operations), based on the
        CFDIs of `start_period_id`..`end_period_id` (YYYYMM calendar months; `end_period_id`
        defaults to the start). Returns the list version date, a summary (analyzed vs matched
        RFCs, CFDI counts by posting status), MXN amounts, exposure percentages and one row per
        matched RFC with its status history. `statuses` defaults to the risk-bearing ones,
        `presunto` and `definitivo`; add `desvirtuado` and/or `sentencia_favorable` to include
        taxpayers that were later cleared."""
        return await client.fetch(
            f"/businesses/{segment(business_id, 'business_id')}/article_69b/matches",
            start_period_id=start_period_id,
            end_period_id=end_period_id,
            statuses=statuses,
            limit=limit,
            offset=offset,
            accounting_firm_id=accounting_firm_id,
        )
