"""Financial reports of a business: trial balance, balance sheet, income statement, VAT."""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from luca_mcp.api import RaiClient
from luca_mcp.tools._common import (
    READ_ONLY,
    AccountingFirmId,
    BusinessId,
    EndPeriodId,
    PeriodId,
    StartPeriodId,
    segment,
)


def register(mcp: FastMCP, client: RaiClient) -> None:
    @mcp.tool(name="get_trial_balance", annotations=READ_ONLY)
    async def get_trial_balance(
        business_id: BusinessId,
        start_period_id: StartPeriodId,
        end_period_id: EndPeriodId = None,
        levels_deep: Annotated[
            str,
            Field(
                description=(
                    'How many levels of the account hierarchy to expand, as text: "1" (the '
                    'default) for top-level accounts only, "2" for two levels, and so on, or '
                    '"all" for the full hierarchy. Start shallow and only go deeper if you need '
                    "more detail."
                )
            ),
        ] = "1",
        only_detail_accounts: Annotated[
            bool,
            Field(
                description=(
                    "When true, returns only leaf (detail) accounts, omitting their parent/"
                    "summary accounts. Default false, which includes accounts at every level "
                    "down to `levels_deep`."
                )
            ),
        ] = False,
        include_zero_balances: Annotated[
            bool,
            Field(
                description=(
                    "When true, includes accounts whose balance is zero for the period. Default "
                    "false, which omits them."
                )
            ),
        ] = False,
        include_pending_entries: Annotated[
            bool,
            Field(
                description=(
                    "When true, also counts draft / awaiting-validation journal entries towards "
                    "the balances, in addition to validated ones. Default false, which excludes "
                    "them."
                )
            ),
        ] = False,
        signed_balances: Annotated[
            bool,
            Field(
                description=(
                    "When true, negates the balance of credit-nature accounts (e.g. liabilities, "
                    "equity, income) so every balance follows one debit-positive sign convention. "
                    "Default false, which reports each account's natural sign."
                )
            ),
        ] = False,
        max_rows: Annotated[
            int,
            Field(
                description=(
                    "Cap on the number of flattened rows returned, for example 200. Default 200, "
                    "maximum 1000. When the result is capped, the response sets `truncated: "
                    "true`, which means the request should be narrowed, not this limit raised "
                    "further."
                )
            ),
        ] = 200,
        accounting_firm_id: AccountingFirmId = None,
    ) -> Any:
        """Trial balance (balanza de comprobación) of a business for one period or a range.

        Periods are `YYYYMM` (13 = annual close). `levels_deep` is "1" (top-level accounts), "2",
        ... or "all"; start shallow and drill down. `only_detail_accounts` returns leaf accounts
        only. `include_pending_entries` also counts draft / awaiting-validation entries (excluded
        by default). `signed_balances` negates credit-nature balances. Rows are flattened with a
        `depth` field and capped at `max_rows` (`truncated: true` tells you to narrow the request).
        `status: "processing"` means Liebre is still recomputing balances; retry in a moment.
        """
        return await client.fetch(
            f"/businesses/{segment(business_id, 'business_id')}/reports/trial_balance",
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
        business_id: BusinessId,
        start_period_id: StartPeriodId,
        end_period_id: EndPeriodId = None,
        accounting_firm_id: AccountingFirmId = None,
    ) -> Any:
        """Balance sheet (estado de situación financiera) at the end of a period (`YYYYMM`), or a
        range with `end_period_id`. Returns assets, liabilities and equity as sections with line
        items, both totals and `balanced` (assets equal liabilities plus equity within one cent)."""
        return await client.fetch(
            f"/businesses/{segment(business_id, 'business_id')}/reports/balance_sheet",
            start_period_id=start_period_id,
            end_period_id=end_period_id,
            accounting_firm_id=accounting_firm_id,
        )

    @mcp.tool(name="get_income_statement", annotations=READ_ONLY)
    async def get_income_statement(
        business_id: BusinessId,
        start_period_id: StartPeriodId,
        end_period_id: EndPeriodId = None,
        accounting_firm_id: AccountingFirmId = None,
    ) -> Any:
        """Income statement (estado de resultados) for a period or a range (`YYYYMM`). Sections:
        ingresos, costo_ventas, utilidad_bruta, gastos_operacion, utilidad_operacion,
        otros_ingresos_gastos, utilidad_antes_rif, rif, utilidad_neta_ejercicio, with line items.
        Liebre may label the column year-to-date (e.g. "YTD-2026")."""
        return await client.fetch(
            f"/businesses/{segment(business_id, 'business_id')}/reports/income_statement",
            start_period_id=start_period_id,
            end_period_id=end_period_id,
            accounting_firm_id=accounting_firm_id,
        )

    @mcp.tool(name="get_vat_determination", annotations=READ_ONLY)
    async def get_vat_determination(
        business_id: BusinessId,
        period_id: PeriodId,
        include_pending_entries: Annotated[
            bool,
            Field(
                description=(
                    "When true, also counts draft / awaiting-validation journal entries towards "
                    "the VAT determination, in addition to validated ones. Default false, which "
                    "excludes them."
                )
            ),
        ] = False,
        accounting_firm_id: AccountingFirmId = None,
    ) -> Any:
        """VAT (IVA) determination of a business for one monthly period (`YYYYMM`), from the ledger:
        VAT collected, creditable, withheld, payable / in favour, plus warnings such as
        `missing_vat_accounts` explained in `notes` (zeros then mean "nothing mapped", not "no VAT").
        Compare with the SAT declaration for the same month from `list_declaraciones` /
        `get_sat_archive`."""
        return await client.fetch(
            f"/businesses/{segment(business_id, 'business_id')}/taxes/vat/{segment(period_id, 'period_id')}",
            include_pending_entries=include_pending_entries,
            accounting_firm_id=accounting_firm_id,
        )
