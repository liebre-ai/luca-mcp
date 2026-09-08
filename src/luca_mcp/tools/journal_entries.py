"""Journal entries (pólizas): search, list per period, detail."""

from __future__ import annotations

import asyncio
import re
import time
from collections import Counter
from typing import Any

from fastmcp import FastMCP

from luca_mcp.errors import INVALID_INPUT, NOT_FOUND, LucaToolError
from luca_mcp.settings import Settings
from luca_mcp.tools._common import (
    READ_ONLY,
    business_scope,
    page,
    platform_url,
    require_optional_str,
    require_str,
    validate_limit,
    validate_offset,
    validate_period,
)
from luca_mcp.tools.accounts import account_index

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
STATUSES = (
    "draft",
    "awaiting_validation",
    "imported_as_valid",
    "auto_validated",
    "user_validated",
    "canceled",
    "trashed",
    "deleted",
)
TYPES = (
    "opening_balance",
    "income",
    "expense",
    "general",
    "order",
    "income_closing",
    "expenses_closing",
)

# The per-period list is unpaginated upstream; keep it briefly so paging is cheap.
PERIOD_LIST_TTL_SECONDS = 60
_period_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_period_locks: dict[str, asyncio.Lock] = {}


def compact_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "journal_entry_id": entry.get("journal_entry_id"),
        "number": entry.get("number"),
        "date": (entry.get("date") or "")[:10] or None,
        "description": entry.get("description"),
        "journal_entry_type": entry.get("journal_entry_type"),
        "journal_entry_status": entry.get("journal_entry_status"),
        "reconciliation_status": entry.get("reconciliation_status"),
        "is_progressive": entry.get("is_progressive"),
        "has_cost_centers": entry.get("has_cost_centers"),
        "tags": entry.get("tags") or [],
        "open_review_items_count": entry.get("open_review_items_count"),
        "cfdi_uuids": sorted(
            {
                str(ld.get("cfdi_uuid"))
                for ld in (entry.get("localized_data") or [])
                if isinstance(ld, dict) and ld.get("cfdi_uuid")
            }
        ),
    }


def _validate_uuid(value: Any, name: str) -> str:
    text = require_str(value, name).strip()
    if not UUID_RE.match(text):
        raise LucaToolError(INVALID_INPUT, f"{name}={value!r} is not a UUID.")
    return text.lower()


def _validate_date(value: Any, name: str) -> str | None:
    text = require_optional_str(value, name)
    if text is None:
        return None
    if not DATE_RE.match(text):
        raise LucaToolError(INVALID_INPUT, f"{name}={value!r} must be an ISO date (YYYY-MM-DD).")
    return text


def _validate_choice(value: Any, name: str, choices: tuple[str, ...]) -> str | None:
    text = require_optional_str(value, name)
    if text is None:
        return None
    if text not in choices:
        raise LucaToolError(INVALID_INPUT, f"{name}={value!r} must be one of {list(choices)}.")
    return text


async def _period_entries(client, ref, period: str) -> list[dict[str, Any]]:
    key = f"{client.identity.cache_key}|{ref.business_id}|{period}"
    cached = _period_cache.get(key)
    if cached and cached[0] > time.time():
        return cached[1]
    if len(_period_locks) > 1000:
        _period_locks.clear()
    lock = _period_locks.setdefault(key, asyncio.Lock())
    async with lock:
        cached = _period_cache.get(key)
        if cached and cached[0] > time.time():
            return cached[1]
        body = await client.get(
            f"/businesses/{ref.business_id}/periods/{period}/journal_entries",
            firm_id=ref.accounting_firm_id,
            what=f"journal entries of {ref.business_id} in {period}",
        )
        entries = list(body.get("data") or []) if isinstance(body, dict) else []
        if len(_period_cache) > 200:
            oldest = min(_period_cache, key=lambda k: _period_cache[k][0])
            _period_cache.pop(oldest, None)
        _period_cache[key] = (time.time() + PERIOD_LIST_TTL_SECONDS, entries)
        return entries


def register(mcp: FastMCP, settings: Settings) -> None:
    @mcp.tool(name="search_journal_entries", annotations=READ_ONLY)
    async def search_journal_entries(
        business_id: str,
        q: str,
        limit: int = 50,
        offset: int = 0,
        accounting_firm_id: str | None = None,
    ) -> dict[str, Any]:
        """Full-text search of journal entries (pólizas) across all periods of a business.

        `q` is required (text matched against descriptions; e.g. a supplier name, "Depreciación",
        an invoice folio). Results are compact (id, number, date, description, type, status,
        CFDI uuids); call `get_journal_entry` for the lines. Paged with `limit` (max 200) and
        `offset`; `total` is the number of matches. To filter by status, type or date within one
        period use `list_journal_entries_for_period`.
        """
        query = require_str(q, "q").strip()
        if len(query) < 2:
            raise LucaToolError(INVALID_INPUT, "q must have at least 2 characters.")
        limit_value = validate_limit(limit, default=50)
        offset_value = validate_offset(offset)
        client, ref = await business_scope(settings, business_id, accounting_firm_id)
        body = await client.get(
            f"/businesses/{ref.business_id}/journal_entries/search",
            firm_id=ref.accounting_firm_id,
            params={"q": query, "limit": limit_value, "offset": offset_value},
            what=f"journal entry search in {ref.business_id}",
        )
        body = body if isinstance(body, dict) else {}
        items = [compact_entry(e) for e in body.get("data") or []]
        total = body.get("total")
        return {
            "business_id": ref.business_id,
            "q": query,
            "items": items,
            "count": len(items),
            "total": total,
            "offset": offset_value,
            "limit": limit_value,
            "has_more": (offset_value + len(items)) < total
            if isinstance(total, int)
            else len(items) == limit_value,
            "platform_url": platform_url(settings, ref.business_id),
        }

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
    ) -> dict[str, Any]:
        """List the journal entries of one period with counts by status and type, then page them.

        Filters: `status` (draft, awaiting_validation, imported_as_valid, auto_validated,
        user_validated, canceled, trashed, deleted), `journal_entry_type` (income, expense,
        general, opening_balance, order, income_closing, expenses_closing), `date_from` /
        `date_to` (YYYY-MM-DD, inclusive), `q` (substring of the description). `summary` counts
        the whole period before filtering, so "how many entries are awaiting validation" needs no
        paging. Sorted by date then number. The period list is cached for up to 60 seconds. Results are compact; use `get_journal_entry` for lines.
        """
        period = validate_period(period_id)
        status_value = _validate_choice(status, "status", STATUSES)
        type_value = _validate_choice(journal_entry_type, "journal_entry_type", TYPES)
        from_value = _validate_date(date_from, "date_from")
        to_value = _validate_date(date_to, "date_to")
        if from_value and to_value and to_value < from_value:
            raise LucaToolError(INVALID_INPUT, "date_to must not be before date_from.")
        needle = (require_optional_str(q, "q") or "").lower()
        limit_value = validate_limit(limit, default=50)
        offset_value = validate_offset(offset)
        client, ref = await business_scope(settings, business_id, accounting_firm_id)
        entries = await _period_entries(client, ref, period)
        summary = {
            "total": len(entries),
            "by_status": dict(Counter(str(e.get("journal_entry_status")) for e in entries)),
            "by_type": dict(Counter(str(e.get("journal_entry_type")) for e in entries)),
        }
        filtered = []
        for entry in entries:
            if status_value and entry.get("journal_entry_status") != status_value:
                continue
            if type_value and entry.get("journal_entry_type") != type_value:
                continue
            day = (entry.get("date") or "")[:10]
            if from_value and day < from_value:
                continue
            if to_value and day > to_value:
                continue
            if needle and needle not in str(entry.get("description") or "").lower():
                continue
            filtered.append(entry)
        filtered.sort(key=lambda e: ((e.get("date") or ""), e.get("number") or 0))
        paged = page([compact_entry(e) for e in filtered], limit_value, offset_value)
        result: dict[str, Any] = {
            "business_id": ref.business_id,
            "period_id": period,
            "filters": {
                "status": status_value,
                "journal_entry_type": type_value,
                "date_from": from_value,
                "date_to": to_value,
                "q": needle or None,
            },
            "summary": summary,
            "items": paged["items"],
            "count": paged["count"],
            "total": paged["total"],
            "offset": paged["offset"],
            "limit": paged["limit"],
            "has_more": paged["has_more"],
            "platform_url": platform_url(settings, ref.business_id),
        }
        if not entries:
            result["message"] = f"Period {period} has no journal entries."
        elif not filtered:
            result["message"] = "No journal entries match these filters in this period."
        return result

    @mcp.tool(name="get_journal_entry", annotations=READ_ONLY)
    async def get_journal_entry(
        business_id: str,
        journal_entry_id: str,
        accounting_firm_id: str | None = None,
    ) -> dict[str, Any]:
        """One journal entry (póliza) with all its lines, account numbers/names and CFDI links.

        `journal_entry_id` is the UUID from search/list results. Lines carry debit/credit in the
        entry currency (and secondary-currency amounts when the business uses dual currency),
        the ledger account number and name, and linked CFDI data. `balance_check` shows whether
        debits equal credits. `updated_at` is the version token Liebre uses for updates.
        """
        entry_id = _validate_uuid(journal_entry_id, "journal_entry_id")
        client, ref = await business_scope(settings, business_id, accounting_firm_id)
        body = await client.get(
            f"/businesses/{ref.business_id}/journal_entries/{entry_id}",
            firm_id=ref.accounting_firm_id,
            what=f"journal entry {entry_id}",
        )
        entries = (
            body if isinstance(body, list) else ([body] if isinstance(body, dict) and body else [])
        )
        if not entries:
            raise LucaToolError(
                NOT_FOUND, f"Journal entry {entry_id} was not found in {ref.business_id}."
            )
        entry = entries[0]
        period = str(entry.get("period_id") or "")
        index: dict[str, dict[str, Any]] = {}
        if period:
            try:
                index = await account_index(client, ref, period)
            except LucaToolError:
                index = {}  # enrichment is best effort
        lines = []
        total_debit = 0.0
        total_credit = 0.0
        for line in entry.get("entry_lines") or []:
            account = index.get(str(line.get("ledger_account_id")), {})
            debit = float(line.get("debit") or 0)
            credit = float(line.get("credit") or 0)
            total_debit += debit
            total_credit += credit
            lines.append(
                {
                    "index": line.get("index"),
                    "ledger_account_id": line.get("ledger_account_id"),
                    "account_number": account.get("number"),
                    "account_name": account.get("name"),
                    "description": line.get("description"),
                    "debit": line.get("debit"),
                    "credit": line.get("credit"),
                    "exchange_rate": line.get("exchange_rate"),
                    "debit_secondary": line.get("debit_secondary"),
                    "credit_secondary": line.get("credit_secondary"),
                    "cost_center_id": line.get("cost_center_id"),
                    "block_status": line.get("block_status"),
                    "cfdis": [
                        {
                            "cfdi_uuid": ld.get("cfdi_uuid"),
                            "cfdi_type": ld.get("cfdi_type"),
                            "cfdi_status": ld.get("cfdi_status"),
                            "cfdi_date": ld.get("cfdi_date"),
                            "serie_folio": ld.get("cfdi_serie_folio"),
                            "counterparty_tax_id": ld.get("counterparty_tax_id"),
                            "total_amount": ld.get("total_amount"),
                        }
                        for ld in (line.get("localized_data") or [])
                        if isinstance(ld, dict)
                    ],
                    "bank_statement_line": line.get("bank_statement_line_data"),
                }
            )
        return {
            "business_id": ref.business_id,
            "journal_entry": {
                "journal_entry_id": entry.get("journal_entry_id"),
                "period_id": period or None,
                "number": entry.get("number"),
                "date": (entry.get("date") or "")[:10] or None,
                "description": entry.get("description"),
                "currency": entry.get("currency"),
                "journal_entry_type": entry.get("journal_entry_type"),
                "journal_entry_sub_type": entry.get("journal_entry_sub_type"),
                "journal_entry_status": entry.get("journal_entry_status"),
                "journal_entry_origin": entry.get("journal_entry_origin"),
                "reconciliation_status": entry.get("reconciliation_status"),
                "is_progressive": entry.get("is_progressive"),
                "tags": entry.get("tags") or [],
                "updated_at": entry.get("updated_at"),
            },
            "lines": lines,
            "line_count": len(lines),
            "balance_check": {
                "total_debit": round(total_debit, 2),
                "total_credit": round(total_credit, 2),
                "balanced": abs(total_debit - total_credit) < 0.005,
            },
            "accounts_resolved": bool(index),
            "platform_url": platform_url(settings, ref.business_id),
        }
