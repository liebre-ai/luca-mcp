"""SAT side: declaraciones (tax filings), SAT archives, documents."""

from __future__ import annotations

from collections import Counter
from typing import Any

from fastmcp import FastMCP

from luca_mcp.settings import Settings
from luca_mcp.tools._common import READ_ONLY, business_scope, platform_url, validate_year


def register(mcp: FastMCP, settings: Settings) -> None:
    @mcp.tool(name="list_declaraciones", annotations=READ_ONLY)
    async def list_declaraciones(
        business_id: str,
        year: int,
        accounting_firm_id: str | None = None,
    ) -> dict[str, Any]:
        """Monthly SAT tax filings (declaraciones) of a business for a year, with their status.

        For each month: the deadline and, per declaration type (e.g. `iva_isr_ieps_monthly`), the
        status (`presented`, `overdue`, `upcoming`, `future`, `not_applicable`), the primary
        filing when known (presentation date, folio, amount paid, acuse link) and the number of
        complementary filings. Acuse links expire after a few minutes; fetch them again if needed.
        Use with `get_vat_determination` to compare declared VAT against the ledger.
        """
        year_value = validate_year(year)
        client, ref = await business_scope(settings, business_id, accounting_firm_id)
        body = await client.get(
            f"/businesses/{ref.business_id}/declaraciones",
            firm_id=ref.accounting_firm_id,
            params={"year": year_value},
            what=f"declaraciones of {ref.business_id} {year_value}",
        )
        body = body if isinstance(body, dict) else {}
        months = []
        status_counter: Counter[str] = Counter()
        for month in body.get("months") or []:
            declarations = []
            for declaration in month.get("declarations") or []:
                primary = declaration.get("primary")
                status = declaration.get("status")
                status_counter[str(status)] += 1
                declarations.append(
                    {
                        "type": declaration.get("type"),
                        "name": declaration.get("name"),
                        "period_type": declaration.get("period_type"),
                        "status": status,
                        "primary": (
                            {
                                "presentation_date": primary.get("presentation_date"),
                                "folio": primary.get("folio"),
                                "importe_pagado": primary.get("importe_pagado"),
                                "acuse_url": primary.get("acuse_url"),
                                "acuse_url_note": "expires within minutes"
                                if primary.get("acuse_url")
                                else None,
                            }
                            if isinstance(primary, dict)
                            else None
                        ),
                        "complementarias_count": len(declaration.get("complementarias") or []),
                    }
                )
            months.append(
                {
                    "month": month.get("month"),
                    "month_label": month.get("month_label"),
                    "period_id": f"{year_value}{int(month.get('month') or 0):02d}"
                    if month.get("month")
                    else None,
                    "status": month.get("status"),
                    "deadline": month.get("deadline"),
                    "declarations": declarations,
                }
            )
        return {
            "business_id": ref.business_id,
            "year": body.get("year", year_value),
            "months": months,
            "summary_by_status": dict(status_counter),
            "platform_url": platform_url(settings, ref.business_id),
        }
