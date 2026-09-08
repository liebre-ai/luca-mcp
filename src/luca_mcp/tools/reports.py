"""Financial reports: trial balance, VAT determination (more to come)."""

from __future__ import annotations

import asyncio
from typing import Any

from fastmcp import FastMCP

from luca_mcp.errors import INVALID_INPUT, LucaToolError
from luca_mcp.liebre.client import LiebreClient
from luca_mcp.settings import Settings
from luca_mcp.tools._common import (
    MAX_ROWS,
    READ_ONLY,
    business_scope,
    platform_url,
    validate_limit,
    validate_period,
)

PROCESSING_BACKOFF = (2.0, 4.0, 8.0)


async def get_report_with_polling(
    client: LiebreClient, path: str, *, firm_id: str, params: dict[str, Any], what: str
) -> tuple[Any, int]:
    """Liebre answers `status: "processing"` while its balance cache rebuilds; poll briefly."""
    body = await client.get(path, firm_id=firm_id, params=params, what=what)
    polls = 0
    for delay in PROCESSING_BACKOFF:
        if not (isinstance(body, dict) and body.get("status") == "processing"):
            break
        await asyncio.sleep(delay)
        polls += 1
        body = await client.get(path, firm_id=firm_id, params=params, what=what)
    return body, polls


def _validate_levels_deep(value: Any) -> str:
    text = str(value if value is not None else "1").strip().lower()
    if text == "all":
        return "all"
    if text.isdigit() and 1 <= int(text) <= 10:
        return text
    raise LucaToolError(
        INVALID_INPUT, f"levels_deep={value!r} must be 'all' or a depth from 1 to 10 (as text)."
    )


def flatten_accounts(
    rows: list[dict[str, Any]],
    *,
    depth: int = 0,
    max_depth: int | None = None,
    out: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Flatten the nested account tree. ``max_depth`` keeps depths 0..max_depth-1 (None = all);
    the API returns the whole subtree whatever ``levels_deep`` says."""
    out = [] if out is None else out
    if max_depth is not None and depth >= max_depth:
        return out
    for row in rows:
        out.append(
            {
                "depth": depth,
                "number": row.get("number"),
                "name": row.get("name"),
                "type": row.get("type"),
                "sub_type": row.get("sub_type"),
                "nature": row.get("nature"),
                "is_leaf": row.get("is_leaf"),
                "beginning_balance": row.get("beginning_balance"),
                "debits": row.get("debits"),
                "credits": row.get("credits"),
                "ending_balance": row.get("ending_balance"),
                "currency": row.get("currency"),
                "has_pending_entry_lines": row.get("has_pending_entry_lines"),
                "has_unreconciled_entry_lines": row.get("has_unreconciled_entry_lines"),
            }
        )
        children = row.get("sub_ledger_accounts") or []
        if children:
            flatten_accounts(children, depth=depth + 1, max_depth=max_depth, out=out)
    return out


def register(mcp: FastMCP, settings: Settings) -> None:
    @mcp.tool(name="get_trial_balance", annotations=READ_ONLY)
    async def get_trial_balance(
        business_id: str,
        start_period_id: str,
        end_period_id: str | None = None,
        levels_deep: str = "1",
        only_detail_accounts: bool = False,
        include_zero_balances: bool = False,
        include_pending_entries: bool = False,
        signed_balances: bool = False,
        max_rows: int = MAX_ROWS,
        accounting_firm_id: str | None = None,
    ) -> dict[str, Any]:
        """Trial balance (balanza de comprobación) of a business for one period or a range.

        Periods are `YYYYMM` (13 = annual close). `levels_deep` is `"1"` (top-level accounts),
        `"2"`, ... or `"all"`; start shallow and drill down. `only_detail_accounts` returns leaf
        accounts only. `include_pending_entries` also counts draft / awaiting-validation entries
        (excluded by default). `signed_balances` negates credit-nature balances. Amounts are in the
        business currency. Rows are flattened with a `depth` field and capped at `max_rows`
        (`truncated: true` tells you to narrow the request). `status: "processing"` means Liebre
        is still recomputing balances; retry in a moment.
        """
        start = validate_period(start_period_id, "start_period_id")
        end = validate_period(end_period_id, "end_period_id", allow_none=True)
        if end and end < start:
            raise LucaToolError(INVALID_INPUT, "end_period_id must not be before start_period_id.")
        levels = _validate_levels_deep(levels_deep)
        cap = validate_limit(max_rows, "max_rows", default=MAX_ROWS, cap=1000)
        client, ref = await business_scope(settings, business_id, accounting_firm_id)
        params = {
            "start_period_id": start,
            "end_period_id": end,
            "levels_deep": levels,
            "only_detail_accounts": only_detail_accounts,
            "include_zero_balances": include_zero_balances,
            "include_pending_entries": include_pending_entries,
            "signed_balances": signed_balances,
        }
        body, polls = await get_report_with_polling(
            client,
            f"/businesses/{ref.business_id}/reports/trial_balance",
            firm_id=ref.accounting_firm_id,
            params=params,
            what=f"trial balance of {ref.business_id} {start}-{end or start}",
        )
        body = body if isinstance(body, dict) else {}
        rows = flatten_accounts(
            body.get("data") or [], max_depth=None if levels == "all" else int(levels)
        )
        truncated = len(rows) > cap
        result: dict[str, Any] = {
            "business_id": ref.business_id,
            "start_period_id": start,
            "end_period_id": end or start,
            "status": body.get("status", "ready"),
            "options": {
                k: v for k, v in params.items() if k not in ("start_period_id", "end_period_id")
            },
            "totals": {
                "beginning_balance": body.get("beginning_balance"),
                "total_debits": body.get("total_debits"),
                "total_credits": body.get("total_credits"),
                "ending_balance": body.get("ending_balance"),
            },
            "rows": rows[:cap],
            "row_count": min(len(rows), cap),
            "total_rows": len(rows),
            "truncated": truncated,
            "platform_url": platform_url(settings, ref.business_id),
        }
        if body.get("reconciliation") is not None:
            result["reconciliation"] = body.get("reconciliation")
        if truncated:
            result["hint"] = (
                f"Showing {cap} of {len(rows)} rows. Use a smaller levels_deep, "
                "only_detail_accounts=true, or raise max_rows (up to 1000)."
            )
        if body.get("status") == "processing":
            result["hint"] = (
                f"Liebre is still recomputing balances (polled {polls} times); the rows may be "
                "incomplete. Retry in a minute."
            )
        return result

    @mcp.tool(name="get_vat_determination", annotations=READ_ONLY)
    async def get_vat_determination(
        business_id: str,
        period_id: str,
        include_pending_entries: bool = False,
        accounting_firm_id: str | None = None,
    ) -> dict[str, Any]:
        """VAT (IVA) determination of a business for one monthly period, from the ledger.

        Returns VAT collected, creditable, withheld, payable / in favour, and warnings such as
        `missing_vat_accounts` when the chart of accounts has no account of the needed VAT type
        (figures are then zero, not "no VAT"). Compare with the SAT declaration for the same
        month from `list_declaraciones`. `period_id` is `YYYYMM`.
        """
        period = validate_period(period_id)
        client, ref = await business_scope(settings, business_id, accounting_firm_id)
        body = await client.get(
            f"/businesses/{ref.business_id}/taxes/vat/determination/{period}",
            firm_id=ref.accounting_firm_id,
            params={"include_pending_entries": include_pending_entries},
            what=f"VAT determination of {ref.business_id} {period}",
        )
        body = body if isinstance(body, dict) else {}
        warnings = body.get("warnings") or []
        notes = []
        for warning in warnings:
            code = warning.get("code") if isinstance(warning, dict) else str(warning)
            if code == "missing_vat_accounts":
                notes.append(
                    "The chart of accounts has no account typed "
                    f"{warning.get('sub_sub_type')!r}; the related figure is 0 because nothing "
                    "is mapped, not because there was no VAT."
                )
            else:
                notes.append(f"Warning from Liebre: {warning}")
        return {
            "business_id": ref.business_id,
            "period_id": period,
            "currency": body.get("currency"),
            "status": body.get("status"),
            "include_pending_entries": include_pending_entries,
            "determination": body.get("determination"),
            "vat_in_favor": body.get("vat_in_favor"),
            "vat_withheld_from_suppliers": body.get("vat_withheld_from_suppliers"),
            "warnings": warnings,
            "notes": notes,
            "platform_url": platform_url(settings, ref.business_id),
        }
