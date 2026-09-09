"""The Luca MCP stdio server: thin tools that forward to rai's MCP-facing endpoints.

Tool logic (validation, shaping, access checks) lives in ant-rai; this process only holds the
user's Luca session and speaks MCP to the agent. Docstrings are what the agent reads, so they stay
as descriptive as the server-side implementation they front.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
from urllib.parse import quote

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from luca_mcp import __version__, oauth
from luca_mcp.api import NOT_LOGGED_IN, LucaError, RaiClient
from luca_mcp.config import Config, load_config
from luca_mcp.prompts import luca as prompts

logger = logging.getLogger("luca_mcp.server")

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
SESSION = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)

INSTRUCTIONS = """You are connected to Luca, the accounting assistant of the Liebre platform, through a local
MCP client that acts on behalf of the logged-in Liebre user. If a tool answers that you are not
logged in, call `login` (it opens the user's browser for the Liebre login) and retry.

Start with `list_businesses` to learn which businesses the user can work on, then pass the chosen
`business_id` to every other tool. Periods are `YYYYMM` strings (13 = annual closing period).
Amounts are numbers in the business currency, exactly as Liebre reports them.

Luca is read-only: it cannot create or change journal entries, periods, accounts, credentials or
filings. When a user asks for a change, say so plainly and point them to the Liebre platform link
returned by the tools. Never guess a business_id: call `list_businesses`."""


_SEGMENT_FORBIDDEN = re.compile(r"[\s/\\?#%]")


def _segment(value: Any, name: str) -> str:
    """An identifier that becomes one path segment of the rai URL. Anything that could change the
    path (slashes, whitespace, query/fragment characters, dot-segments) is rejected here with a
    clear message instead of turning into a request for a different route."""
    if not isinstance(value, str) or not value.strip():
        raise LucaError(
            {
                "error": {
                    "code": "invalid_input",
                    "message": f"{name} is required and must be a non-empty string.",
                }
            }
        )
    text = value.strip()
    if _SEGMENT_FORBIDDEN.search(text) or text.startswith("."):
        raise LucaError(
            {
                "error": {
                    "code": "invalid_input",
                    "message": f"{name} {text[:40]!r} is not a valid identifier.",
                    "hint": "Use an id exactly as returned by the listing tools.",
                }
            }
        )
    return quote(text, safe="")


def create_server(config: Config | None = None) -> FastMCP:
    config = config or load_config()
    client = RaiClient(config)
    mcp = FastMCP("luca", instructions=INSTRUCTIONS, version=__version__, mask_error_details=False)

    async def get(path: str, **params: Any) -> Any:
        return await asyncio.to_thread(client.get, path, params)

    # ------------------------------------------------------------------------------------------
    # Session
    # ------------------------------------------------------------------------------------------

    @mcp.tool(name="login", annotations=SESSION)
    async def login() -> dict[str, Any]:
        """Log in to Luca with your Liebre account. Opens the browser; call this when a tool says you
        are not logged in or your session expired. Blocks until the login completes (up to 5 min).
        Returns who you are logged in as."""
        urls: list[str] = []
        try:
            tokens = await asyncio.to_thread(
                oauth.login, config, open_browser=True, on_url=urls.append
            )
        except oauth.LoginError as exc:
            raise LucaError(
                {
                    "error": {
                        "code": "login_failed",
                        "message": str(exc),
                        "hint": "If no browser opened, open the URL from the message manually.",
                        "authorize_url": urls[0] if urls else None,
                    }
                }
            ) from exc
        me = await get("/whoami")
        return {
            "logged_in": True,
            "session_expires_at": tokens.expires_at,
            **(me if isinstance(me, dict) else {}),
        }

    @mcp.tool(name="logout", annotations=SESSION)
    async def logout() -> dict[str, Any]:
        """Log out of Luca on this machine: revokes the session at the server and deletes the stored
        credentials."""
        tokens = oauth.load_tokens(config)
        if tokens:
            await asyncio.to_thread(oauth.revoke, config, tokens)
        oauth.clear_tokens(config)
        return {"logged_out": True, "logged_in": False, "rai_url": config.rai_url}

    @mcp.tool(name="whoami", annotations=READ_ONLY)
    async def whoami() -> dict[str, Any]:
        """Who am I logged in as, what can I access, and which Luca server is this client using?
        Never fails: when not logged in it says so and tells you to call `login`."""
        tokens = oauth.current_tokens(config)
        if tokens is None:
            return {
                "logged_in": False,
                "rai_url": config.rai_url,
                "client_version": __version__,
                **NOT_LOGGED_IN,
            }
        try:
            me = await get("/whoami")
        except LucaError as exc:
            return {
                "logged_in": False,
                "rai_url": config.rai_url,
                "client_version": __version__,
                **exc.envelope,
            }
        return {
            "logged_in": True,
            "rai_url": config.rai_url,
            "client_version": __version__,
            **(me if isinstance(me, dict) else {"server": me}),
        }

    # ------------------------------------------------------------------------------------------
    # Businesses and periods
    # ------------------------------------------------------------------------------------------

    @mcp.tool(name="list_businesses", annotations=READ_ONLY)
    async def list_businesses(query: str | None = None, limit: int = 50, offset: int = 0) -> Any:
        """List the businesses (companies) the logged-in user can work on, with their firm.

        Call this first; every other tool needs one of these `business_id` values (format `bu-<n>`).
        Returns legal and commercial name, RFC (tax id), status and the accounting firm, favourites
        first. Firm administrators can see hundreds of businesses: use `query` (case-insensitive
        match on id, names or RFC) and `limit`/`offset` (max 200 per page).
        """
        return await get("/businesses", query=query, limit=limit, offset=offset)

    @mcp.tool(name="get_business", annotations=READ_ONLY)
    async def get_business(business_id: str, accounting_firm_id: str | None = None) -> Any:
        """Profile of one business: legal/commercial name, RFC, fiscal regime, currency, status,
        creation date and its last open period. Confirm the company before reporting figures."""
        return await get(
            f"/businesses/{_segment(business_id, 'business_id')}",
            accounting_firm_id=accounting_firm_id,
        )

    @mcp.tool(name="list_periods", annotations=READ_ONLY)
    async def list_periods(
        business_id: str, year: int | None = None, accounting_firm_id: str | None = None
    ) -> Any:
        """List accounting periods of a business with their status.

        Periods are `YYYYMM` strings; month 13 is the annual closing period. Status is one of
        before_beginning_period, not_opened, open, closed; validation status is nothing_pending,
        pending_journal_entries, pending_reconciliation or pending_invoices. Also returns
        `last_open_period`, the period most tools should default to. Filter by `year` to shorten
        the list (Liebre returns a 13-period calendar for any year; look at `open_periods`).
        """
        return await get(
            f"/businesses/{_segment(business_id, 'business_id')}/periods",
            year=year,
            accounting_firm_id=accounting_firm_id,
        )

    # ------------------------------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------------------------------

    @mcp.tool(name="get_trial_balance", annotations=READ_ONLY)
    async def get_trial_balance(
        business_id: str,
        start_period_id: str | int,
        end_period_id: str | int | None = None,
        levels_deep: str = "1",
        only_detail_accounts: bool = False,
        include_zero_balances: bool = False,
        include_pending_entries: bool = False,
        signed_balances: bool = False,
        max_rows: int = 200,
        accounting_firm_id: str | None = None,
    ) -> Any:
        """Trial balance (balanza de comprobación) of a business for one period or a range.

        Periods are `YYYYMM` (13 = annual close). `levels_deep` is "1" (top-level accounts), "2",
        ... or "all"; start shallow and drill down. `only_detail_accounts` returns leaf accounts
        only. `include_pending_entries` also counts draft / awaiting-validation entries (excluded
        by default). `signed_balances` negates credit-nature balances. Rows are flattened with a
        `depth` field and capped at `max_rows` (`truncated: true` tells you to narrow the request).
        `status: "processing"` means Liebre is still recomputing balances; retry in a moment.
        """
        return await get(
            f"/businesses/{_segment(business_id, 'business_id')}/reports/trial_balance",
            start_period_id=start_period_id,
            end_period_id=end_period_id,
            levels_deep=levels_deep,
            only_detail_accounts=only_detail_accounts,
            include_zero_balances=include_zero_balances,
            include_pending_entries=include_pending_entries,
            signed_balances=signed_balances,
            max_rows=max_rows,
            accounting_firm_id=accounting_firm_id,
        )

    @mcp.tool(name="get_balance_sheet", annotations=READ_ONLY)
    async def get_balance_sheet(
        business_id: str,
        start_period_id: str | int,
        end_period_id: str | int | None = None,
        accounting_firm_id: str | None = None,
    ) -> Any:
        """Balance sheet (estado de situación financiera) at the end of a period (`YYYYMM`), or a
        range with `end_period_id`. Returns assets, liabilities and equity as sections with line
        items, both totals and `balanced` (assets equal liabilities plus equity within one cent)."""
        return await get(
            f"/businesses/{_segment(business_id, 'business_id')}/reports/balance_sheet",
            start_period_id=start_period_id,
            end_period_id=end_period_id,
            accounting_firm_id=accounting_firm_id,
        )

    @mcp.tool(name="get_income_statement", annotations=READ_ONLY)
    async def get_income_statement(
        business_id: str,
        start_period_id: str | int,
        end_period_id: str | int | None = None,
        accounting_firm_id: str | None = None,
    ) -> Any:
        """Income statement (estado de resultados) for a period or a range (`YYYYMM`). Sections:
        ingresos, costo_ventas, utilidad_bruta, gastos_operacion, utilidad_operacion,
        otros_ingresos_gastos, utilidad_antes_rif, rif, utilidad_neta_ejercicio, with line items.
        Liebre may label the column year-to-date (e.g. "YTD-2026")."""
        return await get(
            f"/businesses/{_segment(business_id, 'business_id')}/reports/income_statement",
            start_period_id=start_period_id,
            end_period_id=end_period_id,
            accounting_firm_id=accounting_firm_id,
        )

    @mcp.tool(name="get_vat_determination", annotations=READ_ONLY)
    async def get_vat_determination(
        business_id: str,
        period_id: str | int,
        include_pending_entries: bool = False,
        accounting_firm_id: str | None = None,
    ) -> Any:
        """VAT (IVA) determination of a business for one monthly period (`YYYYMM`), from the ledger:
        VAT collected, creditable, withheld, payable / in favour, plus warnings such as
        `missing_vat_accounts` explained in `notes` (zeros then mean "nothing mapped", not "no VAT").
        Compare with the SAT declaration for the same month from `list_declaraciones` /
        `get_sat_archive`."""
        return await get(
            f"/businesses/{_segment(business_id, 'business_id')}/taxes/vat/{_segment(period_id, 'period_id')}",
            include_pending_entries=include_pending_entries,
            accounting_firm_id=accounting_firm_id,
        )

    # ------------------------------------------------------------------------------------------
    # Journal entries
    # ------------------------------------------------------------------------------------------

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
        return await get(
            f"/businesses/{_segment(business_id, 'business_id')}/journal_entries/search",
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
        return await get(
            f"/businesses/{_segment(business_id, 'business_id')}/periods/{_segment(period_id, 'period_id')}/journal_entries",
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
        return await get(
            f"/businesses/{_segment(business_id, 'business_id')}/journal_entries/{_segment(journal_entry_id, 'journal_entry_id')}",
            accounting_firm_id=accounting_firm_id,
        )

    # ------------------------------------------------------------------------------------------
    # SAT side
    # ------------------------------------------------------------------------------------------

    @mcp.tool(name="list_declaraciones", annotations=READ_ONLY)
    async def list_declaraciones(
        business_id: str, year: int, accounting_firm_id: str | None = None
    ) -> Any:
        """Monthly SAT tax filings (declaraciones) of a business for a year: per month the deadline
        and, per declaration type, the status (presented, overdue, upcoming, future,
        not_applicable), the primary filing when known (date, folio, amount paid, acuse link that
        expires within minutes) and the number of complementary filings."""
        return await get(
            f"/businesses/{_segment(business_id, 'business_id')}/declaraciones",
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
        return await get(
            f"/businesses/{_segment(business_id, 'business_id')}/sat_archives",
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
        return await get(
            f"/businesses/{_segment(business_id, 'business_id')}/sat_archives/{_segment(sat_archive_id, 'sat_archive_id') if sat_archive_id else 'latest'}",
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
        return await get(
            f"/businesses/{_segment(business_id, 'business_id')}/article_69b/matches",
            start_period_id=start_period_id,
            end_period_id=end_period_id,
            statuses=statuses,
            limit=limit,
            offset=offset,
            accounting_firm_id=accounting_firm_id,
        )

    @mcp.tool(name="list_documents", annotations=READ_ONLY)
    async def list_documents(
        business_id: str,
        document_type: str | None = None,
        year: int | None = None,
        month: int | None = None,
        limit: int = 50,
        offset: int = 0,
        accounting_firm_id: str | None = None,
    ) -> Any:
        """Fiscal documents stored for a business (SAT acuses, DIOT PDFs, constancia, opinión...).
        Filters: `document_type` (see `available_types`), `year`, `month`. Use `get_document` for a
        temporary download link."""
        return await get(
            f"/businesses/{_segment(business_id, 'business_id')}/documents",
            document_type=document_type,
            year=year,
            month=month,
            limit=limit,
            offset=offset,
            accounting_firm_id=accounting_firm_id,
        )

    @mcp.tool(name="get_document", annotations=READ_ONLY)
    async def get_document(
        business_id: str, document_id: str, accounting_firm_id: str | None = None
    ) -> Any:
        """Metadata and a temporary signed download link (expires within minutes) for one stored
        fiscal document (PDF/XLSX/XML). Call again for a fresh link."""
        return await get(
            f"/businesses/{_segment(business_id, 'business_id')}/documents/{_segment(document_id, 'document_id')}",
            accounting_firm_id=accounting_firm_id,
        )

    prompts.register(mcp)
    return mcp


def run_stdio() -> None:
    logging.basicConfig(level=logging.WARNING)
    create_server().run(transport="stdio")
