"""Fiscal documents stored for a business and their temporary download links."""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from luca_mcp.api import RaiClient
from luca_mcp.tools._common import (
    DESC_YEAR,
    READ_ONLY,
    AccountingFirmId,
    BusinessId,
    Limit,
    Offset,
    segment,
)

DESC_DOCUMENT_TYPE = (
    "Stored-document type to filter by (for example a SAT acuse, DIOT PDF, constancia or opinión). "
    "Call `list_documents` once and check the result's `available_types` for the exact values "
    "available to this business; omit to return documents of every type."
)
DESC_MONTH = (
    "Month as a number from 1 to 12 (for example 9 for September); use together with `year` to "
    "filter documents to that specific month."
)
DESC_DOCUMENT_ID = (
    "Id of the stored document to fetch, exactly as returned by `list_documents`; never guess it."
)


def register(mcp: FastMCP, client: RaiClient) -> None:
    @mcp.tool(name="list_documents", annotations=READ_ONLY)
    async def list_documents(
        business_id: BusinessId,
        document_type: Annotated[str | None, Field(description=DESC_DOCUMENT_TYPE)] = None,
        year: Annotated[int | None, Field(description=DESC_YEAR)] = None,
        month: Annotated[int | None, Field(description=DESC_MONTH)] = None,
        limit: Limit = 50,
        offset: Offset = 0,
        accounting_firm_id: AccountingFirmId = None,
    ) -> Any:
        """Fiscal documents stored for a business (SAT acuses, DIOT PDFs, constancia, opinión...).
        Filters: `document_type` (see `available_types`), `year`, `month`. Use `get_document` for a
        temporary download link."""
        return await client.fetch(
            f"/businesses/{segment(business_id, 'business_id')}/documents",
            document_type=document_type,
            year=year,
            month=month,
            limit=limit,
            offset=offset,
            accounting_firm_id=accounting_firm_id,
        )

    @mcp.tool(name="get_document", annotations=READ_ONLY)
    async def get_document(
        business_id: BusinessId,
        document_id: Annotated[str, Field(description=DESC_DOCUMENT_ID)],
        accounting_firm_id: AccountingFirmId = None,
    ) -> Any:
        """Metadata and a temporary signed download link (expires within minutes) for one stored
        fiscal document (PDF/XLSX/XML). Call again for a fresh link."""
        return await client.fetch(
            f"/businesses/{segment(business_id, 'business_id')}/documents/{segment(document_id, 'document_id')}",
            accounting_firm_id=accounting_firm_id,
        )
