"""The SAT side of a business: monthly filings, SAT archives and the Article 69-B check."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from luca_mcp.api import RaiClient
from luca_mcp.tools._common import READ_ONLY, segment


def register(mcp: FastMCP, client: RaiClient) -> None:
    @mcp.tool(name="list_declaraciones", annotations=READ_ONLY)
    async def list_declaraciones(
        business_id: str, year: int, accounting_firm_id: str | None = None
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
        business_id: str,
        extraction_type: str | None = None,
        status: str | None = None,
        requested_from: str | None = None,
        requested_to: str | None = None,
        limit: int = 50,
        offset: int = 0,
        accounting_firm_id: str | None = None,
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
        business_id: str,
        sat_archive_id: str | None = None,
        extraction_type: str | None = None,
        include_extracted_data: bool = True,
        max_detail_rows: int = 100,
        accounting_firm_id: str | None = None,
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
        business_id: str,
        start_period_id: str,
        end_period_id: str | None = None,
        statuses: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
        accounting_firm_id: str | None = None,
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
