"""Ledger account index: resolves ledger_account_id -> number/name for enrichment.

The chart-of-accounts endpoint returns the whole tree (hundreds of KB); we fetch it once per
(identity, business, period) and keep it for a few minutes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from luca_mcp.liebre.client import LiebreClient
from luca_mcp.tenant import BusinessRef

logger = logging.getLogger("luca_mcp.accounts")

INDEX_TTL_SECONDS = 300
_index_cache: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}
_index_locks: dict[str, asyncio.Lock] = {}


def _flatten(rows: list[dict[str, Any]], out: dict[str, dict[str, Any]], depth: int = 0) -> None:
    for row in rows:
        account_id = row.get("ledger_account_id")
        if account_id:
            out[str(account_id)] = {
                "number": row.get("number"),
                "name": row.get("name"),
                "type": row.get("type"),
                "sub_type": row.get("sub_type"),
                "nature": row.get("nature"),
                "currency": row.get("currency"),
                "depth": depth,
                "is_leaf": not bool(row.get("sub_ledger_accounts")),
            }
        children = row.get("sub_ledger_accounts") or []
        if children:
            _flatten(children, out, depth + 1)


async def account_index(
    client: LiebreClient, ref: BusinessRef, period_id: str
) -> dict[str, dict[str, Any]]:
    key = f"{client.identity.cache_key}|{ref.business_id}|{period_id}"
    cached = _index_cache.get(key)
    if cached and cached[0] > time.time():
        return cached[1]
    if len(_index_locks) > 1000:
        _index_locks.clear()
    lock = _index_locks.setdefault(key, asyncio.Lock())
    async with lock:
        cached = _index_cache.get(key)
        if cached and cached[0] > time.time():
            return cached[1]
        body = await client.get(
            f"/businesses/{ref.business_id}/chart_of_accounts/{period_id}",
            firm_id=ref.accounting_firm_id,
            params={
                "levels_deep": "all",
                "fields": "ledger_account_id,number,name,type,sub_type,nature,currency",
            },
            what=f"chart of accounts of {ref.business_id} for {period_id}",
        )
        index: dict[str, dict[str, Any]] = {}
        rows = body.get("ledger_accounts") if isinstance(body, dict) else None
        _flatten(rows or [], index)
        if len(_index_cache) > 200:
            oldest = min(_index_cache, key=lambda k: _index_cache[k][0])
            _index_cache.pop(oldest, None)
        _index_cache[key] = (time.time() + INDEX_TTL_SECONDS, index)
        logger.info(
            "account index built: business=%s period=%s accounts=%s",
            ref.business_id,
            period_id,
            len(index),
        )
        return index
