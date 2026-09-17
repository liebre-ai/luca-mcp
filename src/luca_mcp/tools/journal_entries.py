"""Journal entries (pólizas): search across periods, list one period, read one entry."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from luca_mcp.api import RaiClient
from luca_mcp.tools._common import READ_ONLY, segment


def register(mcp: FastMCP, client: RaiClient) -> None:
    @mcp.tool(name="search_journal_entries", annotations=READ_ONLY)
    async def search_journal_entries(
        business_id: str,
        q: str,
        limit: int = 50,
        offset: int = 0,
        accounting_firm_id: str | None = None,
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
        business_id: str,
        period_id: str | int,
        status: str | None = None,
        journal_entry_type: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
        accounting_firm_id: str | None = None,
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
        business_id: str, journal_entry_id: str, accounting_firm_id: str | None = None
    ) -> Any:
        """One journal entry (póliza) with all its lines: account number and name, debit/credit in the
        entry currency (and secondary-currency amounts when the business uses dual currency), linked
        CFDIs, and `balance_check` (debits equal credits). `journal_entry_id` is the UUID from
        search/list results."""
        return await client.fetch(
            f"/businesses/{segment(business_id, 'business_id')}/journal_entries/{segment(journal_entry_id, 'journal_entry_id')}",
            accounting_firm_id=accounting_firm_id,
        )
