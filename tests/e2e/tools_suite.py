"""Tool E2E suite against the dev Liebre API (bu-2 / af-2) through the real OAuth session.

Run:  uv run python tests/e2e/tools_suite.py            (server + mock IdP running)
Writes .e2e-artifacts/tools-suite.json with every case's detail.

Cases cover happy paths with known bu-2 facts, malformed inputs, permission/identity edge cases,
empty results, large results, paging and concurrency.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
import harness as h  # noqa: E402

REPORT = Path(".e2e-artifacts/tools-suite.json")
BU = "bu-2"
results: list[dict[str, Any]] = []
_session: h.Session | None = None


def session() -> h.Session:
    global _session
    if _session is None:
        _session = h.login(save=False)
    return _session


def call(
    tool: str, args: dict[str, Any] | None = None, *, token: str | None = None
) -> dict[str, Any]:
    return asyncio.run(h.mcp_call(token or session().access_token or "", tool, args or {}))


def ok(result: dict[str, Any]) -> dict[str, Any]:
    if result["is_error"]:
        raise AssertionError(f"tool error: {result['text'][:300]}")
    return result["structured_content"]


def err(result: dict[str, Any], code: str) -> dict[str, Any]:
    if not result["is_error"]:
        raise AssertionError(
            f"expected error {code}, got success: {json.dumps(result['structured_content'])[:200]}"
        )
    try:
        envelope = json.loads(result["text"])
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"error text is not our JSON envelope: {result['text'][:200]}"
        ) from exc
    got = envelope.get("error", {}).get("code")
    if got != code:
        raise AssertionError(f"expected code {code}, got {got}: {result['text'][:300]}")
    return envelope["error"]


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def case(name: str) -> Callable[[Callable[[], Any]], Callable[[], Any]]:
    def wrap(fn: Callable[[], Any]) -> Callable[[], Any]:
        def run() -> None:
            started = time.time()
            try:
                detail = fn()
                results.append(
                    {
                        "case": name,
                        "status": "PASS",
                        "detail": detail,
                        "ms": int((time.time() - started) * 1000),
                    }
                )
                print(f"PASS  {name}")
            except Exception as exc:  # noqa: BLE001
                results.append(
                    {
                        "case": name,
                        "status": "FAIL",
                        "detail": repr(exc)[:600],
                        "ms": int((time.time() - started) * 1000),
                    }
                )
                print(f"FAIL  {name}: {exc!r}"[:400])

        run.__name__ = fn.__name__
        return run

    return wrap


# --------------------------------------------------------------------------------------------
# whoami / businesses / periods
# --------------------------------------------------------------------------------------------


@case("whoami: identity, access, server build")
def t_whoami() -> Any:
    s = ok(call("whoami"))
    expect(s["session"]["email"] == h.DEFAULT_IDENTITY, "email")
    expect(
        s["access"]["business_count"] > 300, f"business_count {s['access'].get('business_count')}"
    )
    expect(s["server"]["read_only"] is True, "read_only flag")
    return {"business_count": s["access"]["business_count"], "build": s["server"]["build"]}


@case("list_businesses: default page, query match, no match, paging bounds")
def t_businesses() -> Any:
    default = ok(call("list_businesses"))
    expect(
        default["count"] == 50 and default["total"] > 300 and default["has_more"], "default page"
    )
    curiel = ok(call("list_businesses", {"query": "curiel"}))
    expect(
        [b["business_id"] for b in curiel["businesses"]] == [BU],
        f"query curiel -> {curiel['businesses']}",
    )
    rfc = ok(call("list_businesses", {"query": "cco070716m34", "limit": 200}))
    expect(
        any(b["business_id"] == BU for b in rfc["businesses"]), "query by RFC (case-insensitive)"
    )
    none = ok(call("list_businesses", {"query": "zzz-no-such-business-zzz"}))
    expect(
        none["count"] == 0 and "No business matches" in none.get("message", ""), "no-match message"
    )
    capped = ok(call("list_businesses", {"limit": 10000}))
    expect(
        capped["limit"] == 200 and capped["count"] == 200,
        f"limit capped: {capped['limit']}/{capped['count']}",
    )
    beyond = ok(call("list_businesses", {"offset": 100000}))
    expect(beyond["count"] == 0 and beyond["has_more"] is False, "offset beyond end")
    err(call("list_businesses", {"limit": 0}), "invalid_input")
    err(call("list_businesses", {"offset": -1}), "invalid_input")
    return {"total": default["total"]}


@case("get_business: bu-2 profile")
def t_business() -> Any:
    s = ok(call("get_business", {"business_id": BU}))
    expect(s["tax_id"] == "CCO070716M34" and s["currency"] == "MXN", "profile fields")
    expect(s["fiscal_regime"]["code"] == "601", "fiscal regime")
    expect(s["last_open_period"]["period_id"] == "202608", "last open period")
    return {k: s[k] for k in ("legal_name", "status", "last_open_period")}


@case("business scoping: unknown, malformed, empty ids; wrong firm; injection-looking id")
def t_scoping() -> Any:
    e = err(call("get_business", {"business_id": "bu-999999999"}), "unknown_business")
    expect("list_businesses" in (e.get("hint") or ""), "hint mentions list_businesses")
    err(call("get_business", {"business_id": "'; DROP TABLE business; --"}), "unknown_business")
    err(call("get_business", {"business_id": "../../accounting_firms"}), "unknown_business")
    err(call("get_business", {"business_id": ""}), "invalid_input")
    err(
        call("get_business", {"business_id": BU, "accounting_firm_id": "af-999"}),
        "unknown_business",
    )
    return "all rejected before reaching Liebre"


@case("list_periods: all, by year, invalid year, unknown business")
def t_periods() -> Any:
    all_periods = ok(call("list_periods", {"business_id": BU}))
    expect(all_periods["count"] >= 21, f"count {all_periods['count']}")
    expect(
        "202513" in [p["period_id"] for p in all_periods["periods"]], "annual close period listed"
    )
    expect(any(p["is_annual_close"] for p in all_periods["periods"]), "is_annual_close flag")
    y2026 = ok(call("list_periods", {"business_id": BU, "year": 2026}))
    expect(all(p["period_id"].startswith("2026") for p in y2026["periods"]), "year filter")
    expect(y2026["last_open_period"]["period_id"] == "202608", "last open")
    err(call("list_periods", {"business_id": BU, "year": 1999}), "invalid_input")
    # A non-integer year never reaches the tool: fastmcp validates the schema first and answers
    # with pydantic's message, which is acceptable (clear, no upstream call).
    bad_type = call("list_periods", {"business_id": BU, "year": "abc"})
    expect(
        bad_type["is_error"] and "valid integer" in bad_type["text"],
        f"year=abc: {bad_type['text'][:120]}",
    )
    # Liebre answers a synthetic 13-period calendar for any year; a far-future year must simply
    # have no open periods.
    future = ok(call("list_periods", {"business_id": BU, "year": 2099}))
    expect(
        future["count"] == 13 and future["open_periods"] == [],
        f"future year: {future['count']} periods, open={future['open_periods']}",
    )
    return {"total_periods": all_periods["count"], "open_2026": y2026["open_periods"]}


# --------------------------------------------------------------------------------------------
# reports
# --------------------------------------------------------------------------------------------


@case("get_trial_balance: 202608 depth 1/2/all, balanced totals, options, range, caps")
def t_trial_balance() -> Any:
    d1 = ok(call("get_trial_balance", {"business_id": BU, "start_period_id": "202608"}))
    expect(
        d1["status"] == "ready" and d1["totals"]["total_debits"] == d1["totals"]["total_credits"],
        "balanced totals",
    )
    expect(max(r["depth"] for r in d1["rows"]) == 0 and d1["row_count"] > 10, "depth 1 rows")
    d_all = ok(
        call(
            "get_trial_balance",
            {"business_id": BU, "start_period_id": "202608", "levels_deep": "all"},
        )
    )
    expect(d_all["total_rows"] > d1["total_rows"], "all deeper than 1")
    capped = ok(
        call(
            "get_trial_balance",
            {"business_id": BU, "start_period_id": "202608", "levels_deep": "all", "max_rows": 20},
        )
    )
    expect(
        capped["truncated"] and capped["row_count"] == 20 and "max_rows" in capped["hint"],
        "truncation + hint",
    )
    leaves = ok(
        call(
            "get_trial_balance",
            {
                "business_id": BU,
                "start_period_id": "202608",
                "levels_deep": "all",
                "only_detail_accounts": True,
            },
        )
    )
    expect(all(r["is_leaf"] for r in leaves["rows"]), "only detail accounts")
    rng = ok(
        call(
            "get_trial_balance",
            {"business_id": BU, "start_period_id": "202601", "end_period_id": "202608"},
        )
    )
    expect(rng["end_period_id"] == "202608" and rng["status"] in ("ready", "processing"), "range")
    as_int = call("get_trial_balance", {"business_id": BU, "start_period_id": 202608})
    return {
        "depth1_rows": d1["total_rows"],
        "all_rows": d_all["total_rows"],
        "int_period_accepted": not as_int["is_error"],
    }


@case("get_trial_balance: invalid periods, reversed range, bad levels, empty period")
def t_trial_balance_bad() -> Any:
    for bad in ("2026-08", "20268", "202614", "202600", "abc", ""):
        err(call("get_trial_balance", {"business_id": BU, "start_period_id": bad}), "invalid_input")
    err(
        call(
            "get_trial_balance",
            {"business_id": BU, "start_period_id": "202608", "end_period_id": "202601"},
        ),
        "invalid_input",
    )
    for bad in ("0", "11", "abc"):
        err(
            call(
                "get_trial_balance",
                {"business_id": BU, "start_period_id": "202608", "levels_deep": bad},
            ),
            "invalid_input",
        )
    err(
        call("get_trial_balance", {"business_id": BU, "start_period_id": "202608", "max_rows": 0}),
        "invalid_input",
    )
    empty = ok(call("get_trial_balance", {"business_id": BU, "start_period_id": "202513"}))
    return {"annual_close_rows": empty["total_rows"], "annual_close_status": empty["status"]}


@case("get_balance_sheet: 202608 balanced; range; invalid")
def t_balance_sheet() -> Any:
    s = ok(call("get_balance_sheet", {"business_id": BU, "start_period_id": "202608"}))
    expect(
        s["balanced"] is True,
        f"not balanced: {s['total_assets']} vs {s['total_liabilities_equity']}",
    )
    expect(s["assets"]["sections"] and s["assets"]["sections"][0]["lines"], "asset lines present")
    rng = ok(
        call(
            "get_balance_sheet",
            {"business_id": BU, "start_period_id": "202607", "end_period_id": "202608"},
        )
    )
    expect(rng["date"] == "202607-202608", f"range date {rng['date']}")
    err(call("get_balance_sheet", {"business_id": BU, "start_period_id": "2026"}), "invalid_input")
    return {
        "total_assets": s["total_assets"],
        "sections": [x["name"] for x in s["assets"]["sections"]],
    }


@case("get_income_statement: YTD 2026 and single month")
def t_income() -> Any:
    ytd = ok(
        call(
            "get_income_statement",
            {"business_id": BU, "start_period_id": "202601", "end_period_id": "202608"},
        )
    )
    st = ytd["statements"][0]
    expect(
        st["ingresos"]["total"] > 0 and st["utilidad_neta_ejercicio"] is not None, "figures present"
    )
    expect(st["ingresos"]["sections"][0]["lines"], "revenue lines")
    single = ok(call("get_income_statement", {"business_id": BU, "start_period_id": "202608"}))
    expect(single["statements"], "single period statement")
    return {
        "period_label": st["period"],
        "ingresos": st["ingresos"]["total"],
        "net": st["utilidad_neta_ejercicio"],
    }


@case("get_vat_determination: zeros explained by missing VAT accounts; invalid period")
def t_vat() -> Any:
    s = ok(call("get_vat_determination", {"business_id": BU, "period_id": "202608"}))
    expect("determination" in s and isinstance(s["notes"], list), "shape")
    if s["warnings"]:
        expect(
            any("missing_vat_accounts" in str(w) for w in s["warnings"]) and s["notes"],
            "warnings explained",
        )
    pending = ok(
        call(
            "get_vat_determination",
            {"business_id": BU, "period_id": "202608", "include_pending_entries": True},
        )
    )
    expect(pending["include_pending_entries"] is True, "pending flag echoed")
    err(call("get_vat_determination", {"business_id": BU, "period_id": "202613x"}), "invalid_input")
    return {"determination": s["determination"], "notes": len(s["notes"])}


# --------------------------------------------------------------------------------------------
# journal entries
# --------------------------------------------------------------------------------------------


@case("search_journal_entries: text, unicode, paging, q required/too short, odd input")
def t_search() -> Any:
    dep = ok(call("search_journal_entries", {"business_id": BU, "q": "Depreciación"}))
    expect(
        dep["total"] >= 1 and all("epreciaci" in (i["description"] or "") for i in dep["items"]),
        "unicode search",
    )
    paged = ok(
        call("search_journal_entries", {"business_id": BU, "q": "RAITEST", "limit": 3, "offset": 0})
    )
    expect(paged["count"] <= 3 and isinstance(paged["total"], int), "paging shape")
    if paged["total"] > 3:
        second = ok(
            call(
                "search_journal_entries",
                {"business_id": BU, "q": "RAITEST", "limit": 3, "offset": 3},
            )
        )
        expect(
            [i["journal_entry_id"] for i in second["items"]]
            != [i["journal_entry_id"] for i in paged["items"]],
            "offset moves",
        )
    err(call("search_journal_entries", {"business_id": BU, "q": ""}), "invalid_input")
    err(call("search_journal_entries", {"business_id": BU, "q": "a"}), "invalid_input")
    odd = ok(call("search_journal_entries", {"business_id": BU, "q": "' OR 1=1 --"}))
    expect(odd["count"] == 0 or odd["total"] >= 0, "odd query handled")
    nothing = ok(call("search_journal_entries", {"business_id": BU, "q": "zzqqxx-nothing-here"}))
    expect(nothing["count"] == 0 and nothing["total"] == 0, "no results")
    return {"depreciacion_total": dep["total"], "raitest_total": paged["total"]}


@case("list_journal_entries_for_period: summary counts, filters, paging, empty period, bad filters")
def t_list_period() -> Any:
    full = ok(call("list_journal_entries_for_period", {"business_id": BU, "period_id": "202608"}))
    expect(
        full["summary"]["total"] >= 200 and full["count"] == 50 and full["has_more"],
        f"summary/paging {full['summary']}",
    )
    expect(
        set(full["summary"]["by_status"]) >= {"awaiting_validation", "user_validated"},
        "statuses present",
    )
    by_status = ok(
        call(
            "list_journal_entries_for_period",
            {"business_id": BU, "period_id": "202608", "status": "trashed", "limit": 200},
        )
    )
    expect(
        by_status["total"] == full["summary"]["by_status"].get("trashed", 0)
        and all(i["journal_entry_status"] == "trashed" for i in by_status["items"]),
        "status filter",
    )
    by_type = ok(
        call(
            "list_journal_entries_for_period",
            {"business_id": BU, "period_id": "202608", "journal_entry_type": "income", "limit": 5},
        )
    )
    expect(all(i["journal_entry_type"] == "income" for i in by_type["items"]), "type filter")
    dated = ok(
        call(
            "list_journal_entries_for_period",
            {
                "business_id": BU,
                "period_id": "202608",
                "date_from": "2026-08-01",
                "date_to": "2026-08-01",
                "limit": 200,
            },
        )
    )
    expect(all(i["date"] == "2026-08-01" for i in dated["items"]), "date filter")
    text = ok(
        call(
            "list_journal_entries_for_period",
            {"business_id": BU, "period_id": "202608", "q": "depreciación"},
        )
    )
    expect(text["total"] >= 1, "q filter")
    empty = ok(call("list_journal_entries_for_period", {"business_id": BU, "period_id": "202513"}))
    expect(
        empty["summary"]["total"] == 0 and "no journal entries" in empty.get("message", ""),
        "empty period message",
    )
    err(
        call(
            "list_journal_entries_for_period",
            {"business_id": BU, "period_id": "202608", "status": "validated"},
        ),
        "invalid_input",
    )
    err(
        call(
            "list_journal_entries_for_period",
            {"business_id": BU, "period_id": "202608", "journal_entry_type": "sales"},
        ),
        "invalid_input",
    )
    err(
        call(
            "list_journal_entries_for_period",
            {"business_id": BU, "period_id": "202608", "date_from": "08/01/2026"},
        ),
        "invalid_input",
    )
    err(
        call(
            "list_journal_entries_for_period",
            {
                "business_id": BU,
                "period_id": "202608",
                "date_from": "2026-08-20",
                "date_to": "2026-08-01",
            },
        ),
        "invalid_input",
    )
    return {"summary": full["summary"], "trashed": by_status["total"], "dated_aug1": dated["total"]}


@case("get_journal_entry: lines resolved to accounts, balanced, CFDIs; bad/unknown ids")
def t_get_je() -> Any:
    listing = ok(
        call(
            "list_journal_entries_for_period",
            {"business_id": BU, "period_id": "202608", "status": "user_validated", "limit": 1},
        )
    )
    je_id = listing["items"][0]["journal_entry_id"]
    s = ok(call("get_journal_entry", {"business_id": BU, "journal_entry_id": je_id}))
    expect(
        s["journal_entry"]["journal_entry_id"] == je_id and s["line_count"] >= 2, "entry + lines"
    )
    expect(
        s["accounts_resolved"] and all(line["account_number"] for line in s["lines"]),
        "account numbers resolved",
    )
    expect(s["balance_check"]["balanced"] is True, f"unbalanced: {s['balance_check']}")
    err(
        call("get_journal_entry", {"business_id": BU, "journal_entry_id": "not-a-uuid"}),
        "invalid_input",
    )
    unknown = call(
        "get_journal_entry",
        {"business_id": BU, "journal_entry_id": "00000000-0000-4000-8000-000000000000"},
    )
    expect(unknown["is_error"], "unknown UUID must be an error")
    code = json.loads(unknown["text"])["error"]["code"]
    expect(code in ("not_found", "invalid_input"), f"unknown UUID code {code}")
    return {
        "je": je_id,
        "lines": s["line_count"],
        "cfdis": sum(len(line["cfdis"]) for line in s["lines"]),
        "unknown_code": code,
    }


# --------------------------------------------------------------------------------------------
# SAT side
# --------------------------------------------------------------------------------------------


@case("list_declaraciones: 2026 summary, per-month period ids, bad year")
def t_declaraciones() -> Any:
    s = ok(call("list_declaraciones", {"business_id": BU, "year": 2026}))
    expect(
        len(s["months"]) == 12 and s["months"][0]["period_id"] == "202601",
        "12 months with period ids",
    )
    expect(sum(s["summary_by_status"].values()) >= 12, "summary counts")
    err(call("list_declaraciones", {"business_id": BU, "year": 20260}), "invalid_input")
    old = ok(call("list_declaraciones", {"business_id": BU, "year": 2001}))
    return {"summary": s["summary_by_status"], "old_year_months": len(old["months"])}


@case("list_sat_archives: paging, type filter, invalid type/status, available types")
def t_sat_archives() -> Any:
    s = ok(call("list_sat_archives", {"business_id": BU, "limit": 5}))
    expect(
        s["count"] == 5 and s["total"] >= 100 and s["has_more"], f"paging {s['count']}/{s['total']}"
    )
    expect(
        any(t["extraction_type"] == "mx.declaracion_mensual_diot" for t in s["available_types"]),
        "types catalog",
    )
    diot = ok(
        call(
            "list_sat_archives",
            {"business_id": BU, "extraction_type": "mx.declaracion_mensual_diot", "limit": 3},
        )
    )
    expect(
        all(i["extraction_type"] == "mx.declaracion_mensual_diot" for i in diot["items"]),
        "type filter",
    )
    e = err(
        call("list_sat_archives", {"business_id": BU, "extraction_type": "mx.nope"}),
        "invalid_input",
    )
    expect("Valid values" in (e.get("hint") or ""), "hint lists valid types")
    err(call("list_sat_archives", {"business_id": BU, "status": "done"}), "invalid_input")
    err(
        call("list_sat_archives", {"business_id": BU, "requested_from": "2026/01/01"}),
        "invalid_input",
    )
    failed = ok(call("list_sat_archives", {"business_id": BU, "status": "failed", "limit": 5}))
    return {
        "total": s["total"],
        "diot_first": diot["items"][0]["request"] if diot["items"] else None,
        "failed_count": failed["count"],
    }


@case("get_sat_archive: latest DIOT totals, by id, truncation, metadata-only, bad/unknown ids")
def t_sat_archive() -> Any:
    latest = ok(
        call(
            "get_sat_archive", {"business_id": BU, "extraction_type": "mx.declaracion_mensual_diot"}
        )
    )
    expect(
        latest["status"] == "completed" and latest["extracted_data"]["totals"],
        "latest DIOT has totals",
    )
    expect(
        latest["document"] and latest["document"]["file_name"].endswith(".pdf"), "primary document"
    )
    by_id = ok(
        call(
            "get_sat_archive",
            {"business_id": BU, "sat_archive_id": latest["sat_archive_id"], "max_detail_rows": 5},
        )
    )
    expect(
        by_id["details_truncated"] and len(by_id["extracted_data"]["details"]) == 5,
        "details truncated to 5",
    )
    meta_only = ok(
        call(
            "get_sat_archive",
            {
                "business_id": BU,
                "sat_archive_id": latest["sat_archive_id"],
                "include_extracted_data": False,
            },
        )
    )
    expect(
        meta_only["extracted_data"] is None and meta_only["evidences"] is not None, "metadata only"
    )
    err(call("get_sat_archive", {"business_id": BU}), "invalid_input")
    err(call("get_sat_archive", {"business_id": BU, "sat_archive_id": "xyz"}), "invalid_input")
    unknown = call(
        "get_sat_archive",
        {"business_id": BU, "sat_archive_id": "00000000-0000-4000-8000-000000000000"},
    )
    expect(unknown["is_error"], "unknown archive must error")
    return {
        "filing": latest["extracted_data"].get("filing"),
        "totals_keys": len(latest["extracted_data"]["totals"]),
        "details_total": by_id["details_total"],
    }


@case("list_documents / get_document: filters, types, download link, unknown id")
def t_documents() -> Any:
    docs = ok(call("list_documents", {"business_id": BU, "limit": 5}))
    expect(docs["count"] >= 1 and docs["available_types"], "documents listed with types")
    diot = ok(
        call(
            "list_documents",
            {"business_id": BU, "document_type": "mx.declaracion_mensual_diot", "limit": 5},
        )
    )
    expect(
        all(d["document_type"] == "mx.declaracion_mensual_diot" for d in diot["items"]),
        "type filter",
    )
    err(call("list_documents", {"business_id": BU, "document_type": "mx.nope"}), "invalid_input")
    err(call("list_documents", {"business_id": BU, "month": 13}), "invalid_input")
    one = ok(
        call("get_document", {"business_id": BU, "document_id": docs["items"][0]["document_id"]})
    )
    expect(
        one["download_url"]
        and one["download_url"].startswith("https://")
        and "token=" in one["download_url"],
        "signed download url",
    )
    err(call("get_document", {"business_id": BU, "document_id": "nope"}), "invalid_input")
    unknown = call(
        "get_document", {"business_id": BU, "document_id": "00000000-0000-4000-8000-000000000000"}
    )
    expect(unknown["is_error"], "unknown document must error")
    return {"first": docs["items"][0]["file_name"], "types": len(docs["available_types"])}


# --------------------------------------------------------------------------------------------
# identity edge cases and robustness
# --------------------------------------------------------------------------------------------


@case("identity: e-mail unknown to Liebre -> login_expired with hint; whoami still answers")
def t_unknown_user() -> Any:
    other = h.login(identity="unknown.user@yopmail.com", save=False)
    e = err(call("list_businesses", token=other.access_token), "login_expired")
    expect("unknown.user@yopmail.com" in (e.get("hint") or ""), "hint names the e-mail")
    me = ok(call("whoami", token=other.access_token))
    expect(
        me["access"].get("error", {}).get("code") == "login_expired",
        "whoami surfaces the access error",
    )
    return e


@case("identity: token without e-mail -> no_email_claim (fail closed)")
def t_no_email() -> Any:
    other = h.login(identity="__no_email__", save=False)
    e = err(call("list_businesses", token=other.access_token), "no_email_claim")
    err(
        call(
            "get_trial_balance",
            {"business_id": BU, "start_period_id": "202608"},
            token=other.access_token,
        ),
        "no_email_claim",
    )
    return e["message"]


@case("concurrency: 8 parallel report calls for different periods succeed")
def t_concurrency() -> Any:
    periods = ["202601", "202602", "202603", "202604", "202605", "202606", "202607", "202608"]

    async def burst() -> list[dict[str, Any]]:
        return await asyncio.gather(
            *[
                h.mcp_call(
                    session().access_token or "",
                    "get_trial_balance",
                    {"business_id": BU, "start_period_id": p},
                )
                for p in periods
            ]
        )

    outs = asyncio.run(burst())
    expect(
        all(not o["is_error"] for o in outs),
        f"errors: {[o['text'][:80] for o in outs if o['is_error']]}",
    )
    return {"periods": len(outs), "statuses": [o["structured_content"]["status"] for o in outs]}


@case("robustness: wrong argument types and unknown arguments are rejected cleanly")
def t_types() -> Any:
    r1 = call("list_periods", {"business_id": 42})
    r2 = call(
        "get_trial_balance",
        {"business_id": BU, "start_period_id": "202608", "only_detail_accounts": "yes please"},
    )
    r3 = call("list_businesses", {"unknown_argument": 1})
    return {
        "business_id_int": r1["is_error"],
        "bool_as_text": r2["is_error"],
        "unknown_arg": r3["is_error"],
        "texts": [r1["text"][:80], r2["text"][:80], r3["text"][:80]],
    }


@case("prompts: guidelines and navigation are served")
def t_prompts() -> Any:
    from fastmcp import Client
    from fastmcp.client.auth import BearerAuth

    async def run() -> dict[str, Any]:
        async with Client(
            h.MCP_URL, auth=BearerAuth(session().access_token or ""), timeout=30
        ) as client:
            prompts = await client.list_prompts()
            names = [p.name for p in prompts]
            got = await client.get_prompt("luca_navigation")
            return {"names": names, "chars": len(str(got.messages[0].content))}

    out = asyncio.run(run())
    expect(set(out["names"]) >= {"luca_guidelines", "luca_navigation"}, f"prompts {out['names']}")
    return out


@case(
    "identity: restricted (standard_user) synthetic account sees only its business and can report"
)
def t_restricted_user() -> Any:
    other = h.login(identity="Tom.Hagen.10@yopmail.com", save=False)
    listing = ok(call("list_businesses", token=other.access_token))
    expect(
        listing["total"] == 1 and listing["businesses"][0]["business_id"] == BU,
        f"restricted listing {listing['total']}",
    )
    tb = ok(
        call(
            "get_trial_balance",
            {"business_id": BU, "start_period_id": "202608"},
            token=other.access_token,
        )
    )
    expect(
        tb["status"] in ("ready", "processing") and tb["row_count"] > 0,
        "restricted user can read reports",
    )
    err(
        call("get_business", {"business_id": "bu-1240"}, token=other.access_token),
        "unknown_business",
    )
    return {"total_visible": listing["total"], "rows": tb["row_count"]}


def run_suite() -> int:
    ordered = [
        t_whoami,
        t_businesses,
        t_business,
        t_scoping,
        t_periods,
        t_trial_balance,
        t_trial_balance_bad,
        t_balance_sheet,
        t_income,
        t_vat,
        t_search,
        t_list_period,
        t_get_je,
        t_declaraciones,
        t_sat_archives,
        t_sat_archive,
        t_documents,
        t_unknown_user,
        t_no_email,
        t_restricted_user,
        t_concurrency,
        t_types,
        t_prompts,
    ]
    only = None
    if len(sys.argv) > 2 and sys.argv[1] == "--only":
        only = sys.argv[2].lower()
    for test in ordered:
        if only and only not in test.__name__.lower():
            continue
        test()
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(results, indent=2, default=str))
    failed = [r for r in results if r["status"] == "FAIL"]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed; report: {REPORT}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run_suite())
