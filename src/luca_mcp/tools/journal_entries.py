"""Journal entries (pólizas): search across periods, list one period, read one entry."""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from luca_mcp.api import RaiClient
from luca_mcp.tools._common import (
    READ_ONLY,
    AccountingFirmId,
    BusinessId,
    Limit,
    Offset,
    PeriodId,
    segment,
)


def register(mcp: FastMCP, client: RaiClient) -> None:
    @mcp.tool(name="search_journal_entries", annotations=READ_ONLY)
    async def search_journal_entries(
        business_id: BusinessId,
        q: Annotated[
            str,
            Field(
                description=(
                    "The search text, matched against journal entry descriptions across every "
                    "period of the business. Required, at least 2 characters, for example "
                    "`nómina agosto`."
                )
            ),
        ],
        limit: Limit = 50,
        offset: Offset = 0,
        accounting_firm_id: AccountingFirmId = None,
    ) -> Any:
        """Full-text search of journal entries (pólizas) across all periods of a business. `q` is
        required (at least 2 characters; matched against descriptions). Results are compact; call
        `get_journal_entry` for the lines. Paged with `limit` (max 200) and `offset`. To filter by
        status, type or date within one period use `list_journal_entries_for_period`."""
        return await client.fetch(
            f"/businesses/{segment(business_id, 'business_id')}/journal_entries/search",
            q=q,
            limit=limit,
            offset=offset,
            accounting_firm_id=accounting_firm_id,
        )

    @mcp.tool(name="list_journal_entries_for_period", annotations=READ_ONLY)
    async def list_journal_entries_for_period(
        business_id: BusinessId,
        period_id: PeriodId,
        status: Annotated[
            str | None,
            Field(
                description=(
                    "Restricts results to journal entries with this status: one of `draft`, "
                    "`awaiting_validation`, `imported_as_valid`, `auto_validated`, "
                    "`user_validated`, `canceled`, `trashed`, or `deleted`. Omit to include "
                    "entries in every status."
                )
            ),
        ] = None,
        journal_entry_type: Annotated[
            str | None,
            Field(
                description=(
                    "Restricts results to journal entries of this type: one of `income`, "
                    "`expense`, `general`, `opening_balance`, `order`, `income_closing`, or "
                    "`expenses_closing`. Omit to include every type."
                )
            ),
        ] = None,
        date_from: Annotated[
            str | None,
            Field(
                description=(
                    "The earliest entry date to include, as `YYYY-MM-DD` (for example "
                    "2026-08-01), inclusive. Omit for no lower bound."
                )
            ),
        ] = None,
        date_to: Annotated[
            str | None,
            Field(
                description=(
                    "The latest entry date to include, as `YYYY-MM-DD` (for example "
                    "2026-08-31), inclusive. Omit for no upper bound."
                )
            ),
        ] = None,
        q: Annotated[
            str | None,
            Field(
                description=(
                    "An optional substring to match against journal entry descriptions within "
                    "this period, for example `factura`. Omit to include entries regardless of "
                    "description."
                )
            ),
        ] = None,
        limit: Limit = 50,
        offset: Offset = 0,
        accounting_firm_id: AccountingFirmId = None,
    ) -> Any:
        """List the journal entries of one period with counts by status and type, then page them.

        Filters: `status` (draft, awaiting_validation, imported_as_valid, auto_validated,
        user_validated, canceled, trashed, deleted), `journal_entry_type` (income, expense,
        general, opening_balance, order, income_closing, expenses_closing), `date_from` /
        `date_to` (YYYY-MM-DD, inclusive), `q` (substring of the description). `summary` counts the
        whole period before filtering. Sorted by date then number; compact rows.
        """
        return await client.fetch(
            f"/businesses/{segment(business_id, 'business_id')}/periods/{segment(period_id, 'period_id')}/journal_entries",
            status=status,
            journal_entry_type=journal_entry_type,
            date_from=date_from,
            date_to=date_to,
            q=q,
            limit=limit,
            offset=offset,
            accounting_firm_id=accounting_firm_id,
        )

    @mcp.tool(name="get_journal_entry", annotations=READ_ONLY)
    async def get_journal_entry(
        business_id: BusinessId,
        journal_entry_id: Annotated[
            str,
            Field(
                description=(
                    "The journal entry's UUID, exactly as returned by `search_journal_entries` "
                    "or `list_journal_entries_for_period`, for example "
                    "`3fae2b8e-8e0b-4a90-9c1e-6b9f7f2d9a10`."
                )
            ),
        ],
        accounting_firm_id: AccountingFirmId = None,
    ) -> Any:
        """One journal entry (póliza) with all its lines: account number and name, debit/credit in the
        entry currency (and secondary-currency amounts when the business uses dual currency), linked
        CFDIs, and `balance_check` (debits equal credits). `journal_entry_id` is the UUID from
        search/list results."""
        return await client.fetch(
            f"/businesses/{segment(business_id, 'business_id')}/journal_entries/{segment(journal_entry_id, 'journal_entry_id')}",
            accounting_firm_id=accounting_firm_id,
        )
