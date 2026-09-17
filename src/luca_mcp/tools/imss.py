"""The IMSS side of a business: the cédulas de determinación de cuotas archived from IDSE (the
monthly EMA and the bimonthly EBA), per employer registration (registro patronal)."""

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
    RegistroPatronal,
    segment,
)

DESC_WITH_AMOUNTS = (
    "When true, also returns the figures of each archived cédula: `importe_total` (patronal, "
    "obrera, suma), `saldos`, the RCV and INFONAVIT subtotals, cotizantes, dias_cotizados, "
    "creditos, fecha_limite_pago and the IMSS/INFONAVIT proposal folios. Costs one full archive "
    "read per period on the server; leave false to only see what is archived."
)
DESC_TIPO = (
    "Which cédula: `mensual` (EMA, the IMSS cuotas of one month) or `bimestral` (EBA, RCV and "
    "INFONAVIT of one bimestre). Required together with `year` and `month` when "
    "`sat_archive_id` is omitted."
)
DESC_MONTH = (
    "Month 1-12 of the emisión. For `bimestral` the even closing month of the bimestre "
    "(2 = enero-febrero, 4 = marzo-abril, ..., 12 = noviembre-diciembre)."
)
DESC_SAT_ARCHIVE_ID = (
    "Archive id of the cédula as returned by `list_imss_emisiones` or `list_sat_archives`. Omit "
    "to select the newest completed cédula by `tipo`, `year`, `month` and `registro_patronal`."
)
DESC_INCLUDE_WORKERS = (
    "When true, adds the per-worker rows (asegurados) of the cédula: full NSS, name, "
    "contribution, INFONAVIT credit and the cotización rows (movement code, days, salario base). "
    "This is personal data: ask only when the user needs to reconcile the cédula with payroll. "
    "Off by default."
)
DESC_MAX_WORKERS = (
    "Cap on the worker rows returned when `include_workers` is true; default 50, maximum 500. "
    "`workers_truncated: true` in the result means more workers exist than were returned."
)


def register(mcp: FastMCP, client: RaiClient) -> None:
    @mcp.tool(name="list_imss_emisiones", annotations=READ_ONLY)
    async def list_imss_emisiones(
        business_id: BusinessId,
        year: Annotated[int, Field(description=DESC_YEAR)],
        registro_patronal: RegistroPatronal = None,
        with_amounts: Annotated[bool, Field(description=DESC_WITH_AMOUNTS)] = False,
        accounting_firm_id: AccountingFirmId = None,
    ) -> Any:
        """Calendar of the IMSS emisiones archived for a business in a year, per employer
        registration (registro patronal). For each month: the monthly cédula (EMA: the IMSS cuotas
        by branch) and, in even months, the bimonthly one (EBA: RCV and INFONAVIT for the bimestre
        closing that month), from the newest completed archive of each period. Statuses:
        `archived`, `missing` (an earlier month without an archive), `future` (the current month
        and later: the IMSS issues a month's emisión early in the following month), `not_due` (EBA
        in an odd month). Returns archive and document ids per period plus per-registration and
        year summaries; with `with_amounts` the figures too. Never returns worker data. Empty
        when the business has no IMSS archive: emisiones are extracted from the Luca chat or the
        Liebre platform once the IMSS certificate is configured; this tool cannot start one."""
        return await client.fetch(
            f"/businesses/{segment(business_id, 'business_id')}/imss/emisiones",
            year=year,
            registro_patronal=registro_patronal,
            with_amounts=with_amounts,
            accounting_firm_id=accounting_firm_id,
        )

    @mcp.tool(
        name="get_imss_emision",
        annotations=READ_ONLY,
        meta={"anthropic/maxResultSizeChars": 300_000},
    )
    async def get_imss_emision(
        business_id: BusinessId,
        sat_archive_id: Annotated[str | None, Field(description=DESC_SAT_ARCHIVE_ID)] = None,
        tipo: Annotated[str | None, Field(description=DESC_TIPO)] = None,
        year: Annotated[int | None, Field(description=DESC_YEAR)] = None,
        month: Annotated[int | None, Field(description=DESC_MONTH)] = None,
        registro_patronal: RegistroPatronal = None,
        include_workers: Annotated[bool, Field(description=DESC_INCLUDE_WORKERS)] = False,
        max_workers: Annotated[int, Field(description=DESC_MAX_WORKERS)] = 50,
        accounting_firm_id: AccountingFirmId = None,
    ) -> Any:
        """One IMSS cédula de determinación de cuotas as archived from IDSE: the employer
        (`patron`: registro patronal, RFC, razón social, clase and prima de riesgo), the emisión
        (period, `fecha_limite_pago`, cotizantes, días cotizados, salario mínimo and UMA values,
        the IMSS and INFONAVIT proposal folios), the contributions by concept (`conceptos`, MXN:
        `patronal` paid by the employer, `obrera` withheld from workers, `suma`), the portal
        balances (`saldos`), extraction warnings, the files (Excel workbook as `document`, the
        cédula PDF, evidence count; `get_document` mints a download link) and `workers_total`.
        Select by `sat_archive_id`, or by `tipo`, `year`, `month` and, when the business has
        several registrations, `registro_patronal`: the newest completed archive of that period.
        Worker rows only with `include_workers`; CURP and raw source lines are never returned."""
        base = f"/businesses/{segment(business_id, 'business_id')}/imss/emisiones"
        target = segment(sat_archive_id, "sat_archive_id") if sat_archive_id else "latest"
        return await client.fetch(
            f"{base}/{target}",
            tipo=tipo,
            year=year,
            month=month,
            registro_patronal=registro_patronal,
            include_workers=include_workers,
            max_workers=max_workers,
            accounting_firm_id=accounting_firm_id,
        )
