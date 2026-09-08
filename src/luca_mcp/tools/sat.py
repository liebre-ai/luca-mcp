"""SAT side: declaraciones (tax filings), SAT archives (structured extractions), documents."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from fastmcp import FastMCP

from luca_mcp.errors import INVALID_INPUT, NOT_FOUND, LucaToolError
from luca_mcp.settings import Settings
from luca_mcp.tools._common import (
    READ_ONLY,
    business_scope,
    platform_url,
    require_optional_str,
    require_str,
    validate_limit,
    validate_offset,
    validate_year,
)

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ARCHIVE_STATUSES = ("processing", "completed", "failed")
DOWNLOAD_NOTE = (
    "download_url is a signed link that expires within minutes; call again if it stops working"
)


def _uuid(value: Any, name: str) -> str:
    text = require_str(value, name).strip()
    if not UUID_RE.match(text):
        raise LucaToolError(INVALID_INPUT, f"{name}={value!r} is not a UUID.")
    return text.lower()


def _date(value: Any, name: str) -> str | None:
    text = require_optional_str(value, name)
    if text is None:
        return None
    if not DATE_RE.match(text):
        raise LucaToolError(INVALID_INPUT, f"{name}={value!r} must be an ISO date (YYYY-MM-DD).")
    return text


def _file(info: Any) -> dict[str, Any] | None:
    if not isinstance(info, dict):
        return None
    return {
        "sat_archive_file_id": info.get("sat_archive_file_id"),
        "file_name": info.get("file_name"),
        "file_type": info.get("file_type"),
        "file_size": info.get("file_size"),
        "sha256": info.get("sha256"),
        "index": info.get("index"),
    }


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
    ) -> dict[str, Any]:
        """List what Luca extracted from the SAT portal for a business (SAT archives), newest first.

        Each archive is one extraction run: a type such as `mx.declaracion_mensual_diot`,
        `mx.constancia_de_situacion_fiscal` or `mx.opinion_de_cumplimiento`, the request (e.g.
        year/month), status (`processing`, `completed`, `failed`), the primary document and the
        evidence files. `available_types` lists the valid `extraction_type` values. Use
        `get_sat_archive` for the structured `extracted_data` (the declared figures).
        Dates are `YYYY-MM-DD`; paged with `limit` (max 200) and `offset`.
        """
        type_value = require_optional_str(extraction_type, "extraction_type")
        status_value = require_optional_str(status, "status")
        if status_value and status_value not in ARCHIVE_STATUSES:
            raise LucaToolError(INVALID_INPUT, f"status must be one of {list(ARCHIVE_STATUSES)}.")
        from_value = _date(requested_from, "requested_from")
        to_value = _date(requested_to, "requested_to")
        limit_value = validate_limit(limit, default=50)
        offset_value = validate_offset(offset)
        client, ref = await business_scope(settings, business_id, accounting_firm_id)
        base = f"/businesses/{ref.business_id}/sat_archives"
        types_body = await client.get(
            f"{base}/types", firm_id=ref.accounting_firm_id, what="SAT archive types"
        )
        types = (
            [
                {
                    "extraction_type": t.get("sat_archive_extraction_type_id"),
                    "name": t.get("name"),
                    "document_type_id": t.get("document_type_id"),
                }
                for t in (types_body.get("data") or [])
                if isinstance(t, dict)
            ]
            if isinstance(types_body, dict)
            else []
        )
        valid_types = {t["extraction_type"] for t in types}
        if type_value and valid_types and type_value not in valid_types:
            raise LucaToolError(
                INVALID_INPUT,
                f"extraction_type {type_value!r} is unknown.",
                hint=f"Valid values: {sorted(valid_types)}",
            )
        body = await client.get(
            base,
            firm_id=ref.accounting_firm_id,
            params={
                "sat_archive_extraction_type_id": type_value,
                "status": status_value,
                "requested_from": from_value,
                "requested_to": to_value,
                "limit": limit_value,
                "offset": offset_value,
            },
            what=f"SAT archives of {ref.business_id}",
        )
        body = body if isinstance(body, dict) else {}
        items = [
            {
                "sat_archive_id": a.get("sat_archive_id"),
                "extraction_type": a.get("sat_archive_extraction_type_id"),
                "schema_version": a.get("schema_version"),
                "status": a.get("status"),
                "requested_at": a.get("requested_at"),
                "request": a.get("request_data"),
                "document_id": a.get("document_id"),
                "has_document": a.get("has_document"),
                "evidence_count": a.get("evidence_count"),
            }
            for a in (body.get("data") or [])
            if isinstance(a, dict)
        ]
        pagination = body.get("pagination") or {}
        total = pagination.get("total")
        result: dict[str, Any] = {
            "business_id": ref.business_id,
            "filters": {
                "extraction_type": type_value,
                "status": status_value,
                "requested_from": from_value,
                "requested_to": to_value,
            },
            "items": items,
            "count": len(items),
            "total": total,
            "offset": offset_value,
            "limit": limit_value,
            "has_more": bool(pagination.get("has_more")),
            "available_types": types,
            "platform_url": platform_url(settings, ref.business_id),
        }
        if not items:
            result["message"] = (
                "No SAT archives match. Extractions are created from the Liebre platform (SAT section)."
            )
        return result

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
    ) -> dict[str, Any]:
        """One SAT archive with its structured `extracted_data` (what was declared to the SAT).

        Pass `sat_archive_id` (from `list_sat_archives`) or, instead, `extraction_type` to get the
        latest completed archive of that type (e.g. `mx.declaracion_mensual_diot`). The shape of
        `extracted_data` depends on the type and `schema_version`; DIOT archives carry `filing`
        (period, folio, presentation), `totals` (declared VAT figures) and `details` (one row per
        supplier). `details` is capped at `max_detail_rows` (`details_truncated` tells you).
        Set `include_extracted_data=false` for metadata and files only.
        """
        archive_id = _uuid(sat_archive_id, "sat_archive_id") if sat_archive_id else None
        type_value = require_optional_str(extraction_type, "extraction_type")
        if not archive_id and not type_value:
            raise LucaToolError(INVALID_INPUT, "Pass sat_archive_id or extraction_type.")
        cap = validate_limit(max_detail_rows, "max_detail_rows", default=100, cap=2000)
        client, ref = await business_scope(settings, business_id, accounting_firm_id)
        base = f"/businesses/{ref.business_id}/sat_archives"
        if archive_id:
            body = await client.get(
                f"{base}/{archive_id}",
                firm_id=ref.accounting_firm_id,
                what=f"SAT archive {archive_id}",
            )
        else:
            body = await client.get(
                f"{base}/latest",
                firm_id=ref.accounting_firm_id,
                params={"sat_archive_extraction_type_id": type_value},
                what=f"latest SAT archive of type {type_value}",
            )
        if not isinstance(body, dict) or not body.get("sat_archive_id"):
            raise LucaToolError(NOT_FOUND, "No SAT archive found for that id/type.")
        extracted = body.get("extracted_data") if include_extracted_data else None
        details_total = None
        details_truncated = False
        if isinstance(extracted, dict) and isinstance(extracted.get("details"), list):
            details_total = len(extracted["details"])
            if details_total > cap:
                extracted = {**extracted, "details": extracted["details"][:cap]}
                details_truncated = True
        return {
            "business_id": ref.business_id,
            "sat_archive_id": body.get("sat_archive_id"),
            "extraction_type": body.get("sat_archive_extraction_type_id"),
            "schema_version": body.get("schema_version"),
            "status": body.get("status"),
            "source": body.get("source"),
            "requested_at": body.get("requested_at"),
            "request": body.get("request_data"),
            "document": _file(body.get("document")),
            "evidences": [f for f in (_file(e) for e in (body.get("evidences") or [])) if f],
            "extracted_data": extracted,
            "details_total": details_total,
            "details_truncated": details_truncated,
            "platform_url": platform_url(settings, ref.business_id),
        }

    @mcp.tool(name="list_documents", annotations=READ_ONLY)
    async def list_documents(
        business_id: str,
        document_type: str | None = None,
        year: int | None = None,
        month: int | None = None,
        limit: int = 50,
        offset: int = 0,
        accounting_firm_id: str | None = None,
    ) -> dict[str, Any]:
        """Fiscal documents stored for a business (SAT acuses, DIOT PDFs, constancia, opinión...).

        Filters: `document_type` (see `available_types`, e.g. `mx.declaracion_mensual_diot`),
        `year`, `month`. Returns file name, type, size, source and period. Use `get_document`
        for a temporary download link.
        """
        type_value = require_optional_str(document_type, "document_type")
        year_value = validate_year(year, allow_none=True)
        if month is not None and not (1 <= int(month) <= 12):
            raise LucaToolError(INVALID_INPUT, "month must be 1-12.")
        limit_value = validate_limit(limit, default=50)
        offset_value = validate_offset(offset)
        client, ref = await business_scope(settings, business_id, accounting_firm_id)
        base = f"/businesses/{ref.business_id}/documents"
        types_body = await client.get(
            f"{base}/types", firm_id=ref.accounting_firm_id, what="document types"
        )
        types = (
            [
                {"document_type": t.get("document_type"), "periodicity": t.get("periodicity")}
                for t in (types_body.get("data") or [])
                if isinstance(t, dict)
            ]
            if isinstance(types_body, dict)
            else []
        )
        valid = {t["document_type"] for t in types}
        if type_value and valid and type_value not in valid:
            raise LucaToolError(
                INVALID_INPUT,
                f"document_type {type_value!r} is unknown.",
                hint=f"Valid values: {sorted(valid)}",
            )
        body = await client.get(
            base,
            firm_id=ref.accounting_firm_id,
            params={
                "document_type": type_value,
                "year": year_value,
                "month": month,
                "limit": limit_value,
                "offset": offset_value,
            },
            what=f"documents of {ref.business_id}",
        )
        items = [
            {
                "document_id": d.get("id"),
                "file_name": d.get("file_name"),
                "file_type": d.get("file_type"),
                "file_size": d.get("file_size"),
                "document_type": d.get("document_type"),
                "source": d.get("source"),
                "year": d.get("year"),
                "month": d.get("month"),
                "created_at": d.get("created_at"),
            }
            for d in ((body.get("data") or []) if isinstance(body, dict) else [])
            if isinstance(d, dict)
        ]
        result: dict[str, Any] = {
            "business_id": ref.business_id,
            "filters": {"document_type": type_value, "year": year_value, "month": month},
            "items": items,
            "count": len(items),
            "offset": offset_value,
            "limit": limit_value,
            "has_more": len(items) == limit_value,
            "available_types": types,
            "platform_url": platform_url(settings, ref.business_id),
        }
        if not items:
            result["message"] = "No documents match these filters."
        return result

    @mcp.tool(name="get_document", annotations=READ_ONLY)
    async def get_document(
        business_id: str,
        document_id: str,
        accounting_firm_id: str | None = None,
    ) -> dict[str, Any]:
        """Metadata and a temporary download link for one stored fiscal document (PDF/XLSX/XML).

        The link is signed by Liebre and expires within minutes; it can be opened in a browser or
        downloaded with any HTTP client without further authentication. Call again for a new link.
        """
        doc_id = _uuid(document_id, "document_id")
        client, ref = await business_scope(settings, business_id, accounting_firm_id)
        body = await client.get(
            f"/businesses/{ref.business_id}/documents/{doc_id}",
            firm_id=ref.accounting_firm_id,
            what=f"document {doc_id}",
        )
        body = body if isinstance(body, dict) else {}
        if not body.get("id"):
            raise LucaToolError(NOT_FOUND, f"Document {doc_id} was not found in {ref.business_id}.")
        return {
            "business_id": ref.business_id,
            "document_id": body.get("id"),
            "file_name": body.get("file_name"),
            "file_type": body.get("file_type"),
            "file_size": body.get("file_size"),
            "document_type": body.get("document_type"),
            "source": body.get("source"),
            "year": body.get("year"),
            "month": body.get("month"),
            "created_at": body.get("created_at"),
            "download_url": body.get("download_url"),
            "download_note": DOWNLOAD_NOTE,
            "platform_url": platform_url(settings, ref.business_id),
        }
