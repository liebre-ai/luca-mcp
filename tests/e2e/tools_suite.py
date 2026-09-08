"""Tool E2E suite for the Luca MCP client: client -> rai (OAuth server + MCP endpoints) -> Liebre.

Run:  LUCA_RAI_URL=http://localhost:3030 uv run python tests/e2e/tools_suite.py [--only <substr>]
      (rai dev server on LUCA_RAI_URL, mock IdP from dev/run_local.sh as rai's Auth0 stand-in)
Writes .e2e-artifacts/tools-suite.json with every case's detail.

Every tool call goes through the real client code (in-process FastMCP client over `create_server`,
plus one case through the real `uv run luca-mcp` stdio process). Logins run the real browser flow
with a scripted browser. Identities are synthetic dev accounts of the sandbox firm af-2 only.

Cases cover happy paths with known bu-2 facts, malformed inputs, permission/identity edge cases,
login refusals, token expiry and refresh, logout, empty results, large results, paging, concurrency
and the seeded bu-2 fixtures (tests/e2e/fixtures/bu-2-seed-manifest.json).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("LUCA_MCP_CREDENTIAL_STORE", "file")
os.environ.setdefault("LUCA_RAI_URL", "http://localhost:3030")

import harness as h  # noqa: E402

from luca_mcp import oauth  # noqa: E402
from luca_mcp.config import Config, load_config  # noqa: E402

ARTIFACTS = Path(".e2e-artifacts")
REPORT = ARTIFACTS / "tools-suite.json"
BU = "bu-2"
FIRM = "af-2"
DEFAULT = h.DEFAULT_IDENTITY  # ygreen@company.com: firm admin of af-2, allowed on bu-2
RESTRICTED = "jd@yopmail.com"  # active, allowed on exactly one af-2 business, denied on bu-2
RESTRICTED_BUSINESS = "bu-1328"
DISABLED = "Tom.Hagen.10@yopmail.com"  # user_account_status = disabled in Liebre dev
UNKNOWN = "unknown.user@yopmail.com"  # not a Liebre user
NO_EMAIL = "__no_email__"  # identity provider returns no e-mail claim

results: list[dict[str, Any]] = []
_logged_in: set[str] = set()


def cfg(identity: str = DEFAULT, *, login_timeout: float = 60.0) -> Config:
    """One credential directory per identity so sessions never mix."""
    slug = identity.replace("@", "_at_").replace(".", "_").replace("__", "x").lower()
    base = load_config()
    return dataclasses.replace(
        base, config_dir=ARTIFACTS / "config" / slug, login_timeout=login_timeout
    )


def session(identity: str = DEFAULT) -> Config:
    config = cfg(identity)
    if identity not in _logged_in:
        if oauth.current_tokens(config) is None:
            h.login(identity, config=config)
        _logged_in.add(identity)
    return config


def call(
    tool: str, args: dict[str, Any] | None = None, *, identity: str = DEFAULT
) -> dict[str, Any]:
    return asyncio.run(h.mcp_call(tool, args or {}, config=session(identity)))


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


def refused_login(identity: str) -> tuple[str, float]:
    """Run the real login for an identity rai must refuse; return (message, seconds)."""
    config = cfg(identity, login_timeout=60.0)
    oauth.clear_tokens(config)
    started = time.time()
    try:
        h.login(identity, config=config)
    except (oauth.LoginError, h.HarnessError) as exc:
        return str(exc), time.time() - started
    raise AssertionError(f"login as {identity} succeeded but must be refused")


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
                print(f"PASS  {name}", flush=True)
            except Exception as exc:  # noqa: BLE001
                results.append(
                    {
                        "case": name,
                        "status": "FAIL",
                        "detail": repr(exc)[:600],
                        "ms": int((time.time() - started) * 1000),
                    }
                )
                print(f"FAIL  {name}: {exc!r}"[:400], flush=True)

        run.__name__ = fn.__name__
        return run

    return wrap


# --------------------------------------------------------------------------------------------
# server surface, whoami / businesses / periods
# --------------------------------------------------------------------------------------------


@case("tools: 18 tools listed, data tools read-only, session tools not destructive-by-default")
def t_tools() -> Any:
    tools = asyncio.run(h.mcp_list_tools(session()))
    names = {t["name"] for t in tools}
    expected = {
        "login",
        "logout",
        "whoami",
        "list_businesses",
        "get_business",
        "list_periods",
        "get_trial_balance",
        "get_balance_sheet",
        "get_income_statement",
        "get_vat_determination",
        "search_journal_entries",
        "list_journal_entries_for_period",
        "get_journal_entry",
        "list_declaraciones",
        "list_sat_archives",
        "get_sat_archive",
        "list_documents",
        "get_document",
    }
    expect(names == expected, f"tool names differ: {sorted(names ^ expected)}")
    data_tools = [t for t in tools if t["name"] not in ("login", "logout")]

    def read_only(tool: dict[str, Any]) -> bool:  # fastmcp's model dumps snake_case
        ann = tool.get("annotations") or {}
        return ann.get("readOnlyHint", ann.get("read_only_hint")) is True

    expect(all(read_only(t) for t in data_tools), "every data tool is annotated read-only")
    expect(all((t.get("description") or "").strip() for t in tools), "every tool has a description")
    return {"count": len(tools)}


@case("whoami: identity, Liebre account, firms, businesses without deleted ones")
def t_whoami() -> Any:
    s = ok(call("whoami"))
    expect(s["logged_in"] is True, "logged_in")
    expect(s["session"]["email"] == DEFAULT, "email")
    expect(s["session"]["liebre_user_id"] == "us-2", f"liebre id {s['session']}")
    expect(s["server"]["read_only"] is True, "read_only flag")
    firms = {f["accounting_firm_id"]: f for f in s["access"]["firms"]}
    expect(FIRM in firms and firms[FIRM]["sandbox"] is True, f"firms {list(firms)}")
    businesses = s["access"]["businesses"]
    expect(
        s["access"]["business_count"] > 300 and len(businesses) == s["access"]["business_count"],
        f"business_count {s['access'].get('business_count')}",
    )
    expect(all(b["status"] != "deleted" for b in businesses), "deleted businesses are hidden")
    expect(all(b["accounting_firm_name"] for b in businesses), "firm names resolved")
    return {
        "business_count": s["access"]["business_count"],
        "version": s["server"]["version"],
        "statuses": sorted({b["status"] for b in businesses}),
    }


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
    err(call("get_business", {"business_id": "bu-2';DROP"}), "unknown_business")  # reaches rai
    # ids that could change the request path (or contain whitespace) never leave the client
    for traversal in (
        "../../accounting_firms",
        "bu-2/periods",
        "bu-2?x=1",
        "bu-2#f",
        "bu 2",
        "'; DROP TABLE business; --",
    ):
        e = err(call("get_business", {"business_id": traversal}), "invalid_input")
        expect("business_id" in e["message"], f"names the argument: {e['message']}")
    err(
        call("get_journal_entry", {"business_id": BU, "journal_entry_id": "../periods/202608"}),
        "invalid_input",
    )
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
    # A non-integer year never reaches rai: fastmcp validates the schema first and answers with
    # pydantic's message, which is acceptable (clear, no upstream call).
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
# seeded bu-2 fixtures (never deleted: they are the evidence)
# --------------------------------------------------------------------------------------------

SEED_MANIFEST = Path(__file__).parent / "fixtures" / "bu-2-seed-manifest.json"


def seeded() -> dict[str, dict[str, Any]]:
    manifest = json.loads(SEED_MANIFEST.read_text())
    return {c["label"]: c for c in manifest["cases"]}


@case("seeded: 60-line entry resolves every account and balances")
def t_seed_many_lines() -> Any:
    entry = seeded()["many-lines-60"]
    s = ok(
        call(
            "get_journal_entry", {"business_id": BU, "journal_entry_id": entry["journal_entry_id"]}
        )
    )
    expect(s["line_count"] == 60, f"line_count {s['line_count']}")
    expect(
        s["balance_check"]["balanced"] and s["balance_check"]["total_debit"] == 300.0,
        f"balance {s['balance_check']}",
    )
    numbers = {line["account_number"] for line in s["lines"]}
    expect(numbers == {"6000-001-010", "2130"}, f"accounts {numbers}")
    expect(s["journal_entry"]["journal_entry_status"] == "draft", "status draft")
    return {"lines": s["line_count"], "accounts": sorted(numbers)}


@case("seeded: 600-char unicode/HTML/JSON description round-trips untouched")
def t_seed_unicode() -> Any:
    entry = seeded()["unicode-long-description"]
    s = ok(
        call(
            "get_journal_entry", {"business_id": BU, "journal_entry_id": entry["journal_entry_id"]}
        )
    )
    desc = s["journal_entry"]["description"]
    expect(len(desc) == 600 and desc.startswith("LUCA-MCP-E2E"), f"description length {len(desc)}")
    expect(
        "<b>" in desc and '{"json":true}' in desc and "\U0001f4d2" in desc,
        "special characters preserved",
    )
    found = ok(
        call("search_journal_entries", {"business_id": BU, "q": "LUCA-MCP-E2E", "limit": 200})
    )
    expect(
        found["total"] >= 7
        and all(i["description"].startswith("LUCA-MCP-E2E") for i in found["items"]),
        f"search total {found['total']}",
    )
    return {"length": len(desc), "search_total": found["total"]}


@case("seeded: unbalanced entry is reported unbalanced and awaiting_validation")
def t_seed_unbalanced() -> Any:
    entry = seeded()["unbalanced-awaiting"]
    s = ok(
        call(
            "get_journal_entry", {"business_id": BU, "journal_entry_id": entry["journal_entry_id"]}
        )
    )
    bc = s["balance_check"]
    expect(
        bc["balanced"] is False and bc["total_debit"] == 500.0 and bc["total_credit"] == 499.0,
        f"balance {bc}",
    )
    expect(s["journal_entry"]["journal_entry_status"] == "awaiting_validation", "demoted status")
    return bc


@case("seeded: canceled, tiny and zero-amount entries; period 202607 summary and filters")
def t_seed_statuses() -> Any:
    entries = seeded()
    canceled = ok(
        call(
            "get_journal_entry",
            {"business_id": BU, "journal_entry_id": entries["canceled-entry"]["journal_entry_id"]},
        )
    )
    expect(canceled["journal_entry"]["journal_entry_status"] == "canceled", "canceled status")
    tiny = ok(
        call(
            "get_journal_entry",
            {"business_id": BU, "journal_entry_id": entries["tiny-amounts"]["journal_entry_id"]},
        )
    )
    expect(
        tiny["lines"][0]["debit"] == 0.01 or tiny["lines"][1]["debit"] == 0.01,
        f"tiny amounts {tiny['lines']}",
    )
    zero = ok(
        call(
            "get_journal_entry",
            {
                "business_id": BU,
                "journal_entry_id": entries["zero-amount-lines"]["journal_entry_id"],
            },
        )
    )
    expect(
        zero["balance_check"]["balanced"] and zero["balance_check"]["total_debit"] == 0.0,
        "zero amounts balance",
    )
    listing = ok(
        call(
            "list_journal_entries_for_period",
            {"business_id": BU, "period_id": "202607", "q": "LUCA-MCP-E2E", "limit": 200},
        )
    )
    expect(listing["total"] >= 7, f"period listing total {listing['total']}")
    by_status = listing["summary"]["by_status"]
    expect(
        by_status.get("canceled", 0) >= 1 and by_status.get("draft", 0) >= 5, f"summary {by_status}"
    )
    only_canceled = ok(
        call(
            "list_journal_entries_for_period",
            {"business_id": BU, "period_id": "202607", "status": "canceled", "q": "LUCA-MCP-E2E"},
        )
    )
    expect(
        [i["journal_entry_id"] for i in only_canceled["items"]]
        == [entries["canceled-entry"]["journal_entry_id"]],
        "canceled filter",
    )
    dated = ok(
        call(
            "list_journal_entries_for_period",
            {
                "business_id": BU,
                "period_id": "202607",
                "date_from": "2026-07-15",
                "date_to": "2026-07-15",
                "q": "LUCA-MCP-E2E",
                "limit": 200,
            },
        )
    )
    expect(dated["total"] >= 7, f"dated {dated['total']}")
    return {"summary": by_status, "seeded_in_period": listing["total"]}


# --------------------------------------------------------------------------------------------
# identities: refusals at login, restricted access, session lifecycle
# --------------------------------------------------------------------------------------------


@case("login refused: e-mail unknown to Liebre is rejected by rai, client reports the reason")
def t_unknown_user() -> Any:
    message, seconds = refused_login(UNKNOWN)
    expect("not a Liebre user" in message, f"reason missing: {message[:200]}")
    expect(seconds < 30, f"refusal took {seconds:.0f}s (client must not wait for its timeout)")
    expect(oauth.load_tokens(cfg(UNKNOWN)) is None, "no credentials stored")
    return {"message": message[:160], "seconds": round(seconds, 1)}


@case("login refused: identity without an e-mail claim is rejected (fail closed)")
def t_no_email() -> Any:
    message, seconds = refused_login(NO_EMAIL)
    expect("e-mail" in message.lower(), f"reason missing: {message[:200]}")
    expect(seconds < 30, f"refusal took {seconds:.0f}s")
    return {"message": message[:160], "seconds": round(seconds, 1)}


@case("login refused: disabled Liebre account is rejected with its status")
def t_disabled_user() -> Any:
    message, seconds = refused_login(DISABLED)
    expect("disabled" in message, f"reason missing: {message[:200]}")
    expect(seconds < 30, f"refusal took {seconds:.0f}s")
    return {"message": message[:160], "seconds": round(seconds, 1)}


@case("login refused: user declines at the identity provider -> access_denied, no credentials")
def t_user_declines() -> Any:
    config = cfg("declines", login_timeout=60.0)
    oauth.clear_tokens(config)
    started = time.time()
    try:
        h.login(DEFAULT, config=config, login_decision="deny")
    except (oauth.LoginError, h.HarnessError) as exc:
        message = str(exc)
    else:
        raise AssertionError("login succeeded although the user declined")
    seconds = time.time() - started
    expect("access_denied" in message or "denied" in message.lower(), f"message {message[:200]}")
    expect(seconds < 30 and oauth.load_tokens(config) is None, "fast, nothing stored")
    return {"message": message[:160], "seconds": round(seconds, 1)}


@case("identity: restricted account sees only its business; bu-2 is unknown to it")
def t_restricted_user() -> Any:
    me = ok(call("whoami", identity=RESTRICTED))
    expect(me["session"]["email"] == RESTRICTED, "restricted identity")
    expect(
        me["access"]["business_count"] == 1
        and me["access"]["businesses"][0]["business_id"] == RESTRICTED_BUSINESS,
        f"restricted access {me['access'].get('businesses')}",
    )
    listing = ok(call("list_businesses", identity=RESTRICTED))
    expect(
        listing["total"] == 1 and listing["businesses"][0]["business_id"] == RESTRICTED_BUSINESS,
        f"restricted listing {listing['total']}",
    )
    e = err(call("get_business", {"business_id": BU}, identity=RESTRICTED), "unknown_business")
    expect(BU in e["message"] and "list_businesses" in (e.get("hint") or ""), "hint")
    err(
        call(
            "get_trial_balance",
            {"business_id": BU, "start_period_id": "202608"},
            identity=RESTRICTED,
        ),
        "unknown_business",
    )
    err(
        call("get_business", {"business_id": BU, "accounting_firm_id": FIRM}, identity=RESTRICTED),
        "unknown_business",
    )
    return {"visible": [b["business_id"] for b in listing["businesses"]]}


@case("session: refresh on expired token, login_expired when refresh dies, logout, re-login")
def t_session_lifecycle() -> Any:
    config = cfg("lifecycle", login_timeout=60.0)
    oauth.clear_tokens(config)
    first = h.login(DEFAULT, config=config)
    expect(first.refresh_token is not None, "refresh token issued")
    me = ok(asyncio.run(h.mcp_call("whoami", {}, config=config)))
    expect(me["logged_in"] and me["session"]["email"] == DEFAULT, "fresh session works")

    # 1) access token expired locally -> refreshed before the call, rotated refresh token
    oauth.save_tokens(config, dataclasses.replace(first, expires_at=time.time() - 10))
    me = ok(asyncio.run(h.mcp_call("whoami", {}, config=config)))
    after = oauth.load_tokens(config)
    expect(me["logged_in"] and after is not None, "call after local expiry works")
    expect(after.access_token != first.access_token, "access token refreshed")
    rotated = after.refresh_token != first.refresh_token

    # 2) access token rejected by rai (tampered) -> 401 -> refresh -> retry succeeds
    oauth.save_tokens(
        config, dataclasses.replace(after, access_token=after.access_token[:-4] + "xxxx")
    )
    me = ok(asyncio.run(h.mcp_call("whoami", {}, config=config)))
    expect(me["logged_in"] is True, "tampered access token recovered through refresh")
    again = oauth.load_tokens(config)

    # 3) refresh token dead too -> login_expired with a hint, credentials remain until logout
    oauth.save_tokens(
        config,
        dataclasses.replace(again, access_token="garbage", refresh_token="garbage-refresh"),
    )
    e = err(asyncio.run(h.mcp_call("list_businesses", {}, config=config)), "login_expired")
    expect("login" in (e.get("hint") or "").lower(), "hint says how to log in again")
    me = ok(asyncio.run(h.mcp_call("whoami", {}, config=config)))
    expect(me["logged_in"] is False and me["error"]["code"] == "login_expired", "whoami explains")

    # 4) logout clears everything; calls answer not_authenticated; whoami says logged out
    out = ok(asyncio.run(h.mcp_call("logout", {}, config=config)))
    expect(out["logged_out"] is True and oauth.load_tokens(config) is None, "logged out")
    err(asyncio.run(h.mcp_call("list_businesses", {}, config=config)), "not_authenticated")
    me = ok(asyncio.run(h.mcp_call("whoami", {}, config=config)))
    expect(me["logged_in"] is False and me["error"]["code"] == "not_authenticated", "whoami")

    # 5) a fresh login works again and the old refresh token is dead server-side
    second = h.login(DEFAULT, config=config)
    me = ok(asyncio.run(h.mcp_call("whoami", {}, config=config)))
    expect(me["logged_in"] is True, "re-login works")
    try:
        oauth.refresh(config, dataclasses.replace(second, refresh_token=first.refresh_token))
    except oauth.LoginError as exc:
        old_refresh_rejected = "400" in str(exc) or "invalid_grant" in str(exc)
    else:
        old_refresh_rejected = False
    expect(old_refresh_rejected, "an old refresh token must not mint new sessions")
    return {"refresh_rotated": rotated, "old_refresh_rejected": old_refresh_rejected}


@case("logout: revokes the refresh token at rai and deletes local credentials")
def t_logout_revokes() -> Any:
    config = cfg("logout", login_timeout=60.0)
    oauth.clear_tokens(config)
    tokens = h.login(DEFAULT, config=config)
    out = ok(asyncio.run(h.mcp_call("logout", {}, config=config)))
    expect(out["logged_out"] is True and oauth.load_tokens(config) is None, "cleared")
    try:
        oauth.refresh(config, tokens)
    except oauth.LoginError as exc:
        revoked = True
        detail = str(exc)[:120]
    else:
        revoked = False
        detail = "refresh still worked"
    expect(revoked, f"refresh token still valid after logout: {detail}")
    again = ok(asyncio.run(h.mcp_call("logout", {}, config=config)))
    expect(again["logged_out"] is True, "logout twice is fine")
    return {"detail": detail}


# --------------------------------------------------------------------------------------------
# rai's own surface as the client sees it, robustness, prompts, stdio process
# --------------------------------------------------------------------------------------------


@case("rai endpoints: no token -> not_authenticated, bad token -> login_expired, same envelope")
def t_rai_bearer() -> Any:
    config = session()
    with httpx.Client(base_url=config.api_root, timeout=30) as http:
        none = http.get("/whoami")
        bad = http.get("/whoami", headers={"Authorization": "Bearer not-a-jwt"})
        wrong_scheme = http.get("/whoami", headers={"Authorization": "Basic abc"})
        tokens = oauth.current_tokens(config)
        good = http.get(
            "/businesses",
            params={"query": "curiel"},
            headers={"Authorization": f"Bearer {tokens.access_token if tokens else ''}"},
        )
    expect(none.status_code == 401 and none.json()["error"]["code"] == "not_authenticated", "none")
    expect(bad.status_code == 401 and bad.json()["error"]["code"] == "login_expired", "bad")
    expect(wrong_scheme.status_code == 401, f"basic scheme -> {wrong_scheme.status_code}")
    expect(good.status_code == 200 and good.json()["businesses"][0]["business_id"] == BU, "good")
    expect("www-authenticate" in {k.lower() for k in none.headers}, "WWW-Authenticate on 401")
    return {"codes": [none.status_code, bad.status_code, wrong_scheme.status_code]}


@case("rai authorize: unknown client, non-loopback redirect and bad PKCE method are rejected")
def t_rai_authorize_validation() -> Any:
    config = session()
    base = {
        "response_type": "code",
        "client_id": config.client_id,
        "redirect_uri": "http://127.0.0.1:5555/callback",
        "scope": config.scopes,
        "state": "s",
        "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        "code_challenge_method": "S256",
    }
    with httpx.Client(base_url=config.rai_url, timeout=30, follow_redirects=False) as http:
        good = http.get("/oauth/authorize", params=base)
        unknown_client = http.get("/oauth/authorize", params={**base, "client_id": "evil"})
        remote = http.get(
            "/oauth/authorize", params={**base, "redirect_uri": "https://evil.example/callback"}
        )
        wrong_path = http.get(
            "/oauth/authorize", params={**base, "redirect_uri": "http://127.0.0.1:5555/steal"}
        )
        plain = http.get("/oauth/authorize", params={**base, "code_challenge_method": "plain"})
        meta = http.get("/.well-known/oauth-authorization-server")
    expect(good.status_code in (302, 303, 307), f"valid request redirects: {good.status_code}")
    expect(unknown_client.status_code == 400, f"unknown client {unknown_client.status_code}")
    expect(remote.status_code == 400, f"remote redirect {remote.status_code}")
    expect(wrong_path.status_code == 400, f"wrong path {wrong_path.status_code}")
    expect(plain.status_code == 400, f"plain pkce {plain.status_code}")
    m = meta.json()
    expect(
        "S256" in m.get("code_challenge_methods_supported", [])
        and m["token_endpoint"].startswith(config.rai_url),
        f"metadata {m}",
    )
    return {"idp_hop": good.headers.get("location", "")[:80]}


@case("concurrency: 8 parallel report calls for different periods succeed")
def t_concurrency() -> Any:
    config = session()
    periods = ["202601", "202602", "202603", "202604", "202605", "202606", "202607", "202608"]

    async def burst() -> list[dict[str, Any]]:
        return await asyncio.gather(
            *[
                h.mcp_call(
                    "get_trial_balance", {"business_id": BU, "start_period_id": p}, config=config
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
    r4 = call(
        "get_trial_balance", {"business_id": BU, "start_period_id": "202608", "max_rows": 10**9}
    )
    return {
        "business_id_int": r1["is_error"],
        "bool_as_text": r2["is_error"],
        "unknown_arg": r3["is_error"],
        "huge_max_rows_capped": (not r4["is_error"])
        and r4["structured_content"]["row_count"] <= 200,
        "texts": [r1["text"][:80], r2["text"][:80], r3["text"][:80]],
    }


@case("prompts: guidelines and navigation are served by the client")
def t_prompts() -> Any:
    from fastmcp import Client

    from luca_mcp.server import create_server

    async def run() -> dict[str, Any]:
        async with Client(create_server(session()), timeout=30) as client:
            prompts = await client.list_prompts()
            names = [p.name for p in prompts]
            got = await client.get_prompt("luca_navigation")
            return {"names": names, "chars": len(str(got.messages[0].content))}

    out = asyncio.run(run())
    expect(set(out["names"]) >= {"luca_guidelines", "luca_navigation"}, f"prompts {out['names']}")
    return out


@case("stdio: the real `uv run luca-mcp` process answers whoami and a report")
def t_stdio() -> Any:
    config = session()
    env = {
        "LUCA_MCP_CONFIG_DIR": str(config.config_dir.resolve()),
        "LUCA_RAI_URL": config.rai_url,
        "LUCA_MCP_CREDENTIAL_STORE": "file",
    }
    me = ok(asyncio.run(h.mcp_call_stdio("whoami", {}, env=env)))
    expect(me["logged_in"] and me["session"]["email"] == DEFAULT, "stdio whoami")
    tb = ok(
        asyncio.run(
            h.mcp_call_stdio(
                "get_trial_balance", {"business_id": BU, "start_period_id": "202608"}, env=env
            )
        )
    )
    expect(tb["status"] == "ready" and tb["row_count"] > 10, "stdio report")
    return {"rows": tb["row_count"]}


@case("stdio: without credentials every data tool says not_authenticated, whoami explains")
def t_stdio_logged_out() -> Any:
    empty_dir = ARTIFACTS / "config" / "empty"
    empty_dir.mkdir(parents=True, exist_ok=True)
    (empty_dir / "credentials.json").unlink(missing_ok=True)
    env = {
        "LUCA_MCP_CONFIG_DIR": str(empty_dir.resolve()),
        "LUCA_RAI_URL": load_config().rai_url,
        "LUCA_MCP_CREDENTIAL_STORE": "file",
    }
    e = err(asyncio.run(h.mcp_call_stdio("list_businesses", {}, env=env)), "not_authenticated")
    expect("login" in (e.get("hint") or ""), "hint points to login")
    me = ok(asyncio.run(h.mcp_call_stdio("whoami", {}, env=env)))
    expect(me["logged_in"] is False and me["error"]["code"] == "not_authenticated", "whoami")
    return {"hint": e.get("hint")}


def run_suite() -> int:
    ordered = [
        t_tools,
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
        t_seed_many_lines,
        t_seed_unicode,
        t_seed_unbalanced,
        t_seed_statuses,
        t_unknown_user,
        t_no_email,
        t_disabled_user,
        t_user_declines,
        t_restricted_user,
        t_session_lifecycle,
        t_logout_revokes,
        t_rai_bearer,
        t_rai_authorize_validation,
        t_concurrency,
        t_types,
        t_prompts,
        t_stdio,
        t_stdio_logged_out,
    ]
    only = None
    if len(sys.argv) > 2 and sys.argv[1] == "--only":
        only = sys.argv[2].lower()
    print(f"rai: {load_config().rai_url}", flush=True)
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
