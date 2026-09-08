"""Tenant context: which firms and businesses the caller may act on.

Tokens carry no firm/business claims, so per request we ask Liebre what the user can see:
``GET /accounting_firms`` (no firm header, by API contract) and ``GET /businesses`` per firm
(filtered by Liebre to businesses where the user is ``allowed``). The result is cached briefly per
identity; the cache is an optimisation only, Liebre re-checks every call.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from luca_mcp.errors import AMBIGUOUS_FIRM, UNKNOWN_BUSINESS, LucaToolError
from luca_mcp.liebre.client import LiebreClient
from luca_mcp.settings import Settings

logger = logging.getLogger("luca_mcp.tenant")


@dataclass(frozen=True)
class BusinessRef:
    business_id: str
    accounting_firm_id: str
    legal_name: str | None
    commercial_name: str | None
    tax_id: str | None
    status: str | None
    favorite: bool = False

    def summary(self) -> dict[str, Any]:
        return {
            "business_id": self.business_id,
            "accounting_firm_id": self.accounting_firm_id,
            "legal_name": self.legal_name,
            "commercial_name": self.commercial_name,
            "tax_id": self.tax_id,
            "status": self.status,
            "favorite": self.favorite,
        }


@dataclass
class TenantContext:
    email: str
    firms: list[dict[str, Any]] = field(default_factory=list)
    businesses: dict[str, list[BusinessRef]] = field(default_factory=dict)  # business_id -> refs
    problems: list[str] = field(default_factory=list)
    loaded_at: float = field(default_factory=time.time)

    @property
    def business_list(self) -> list[BusinessRef]:
        return [ref for refs in self.businesses.values() for ref in refs]

    def resolve(self, business_id: str, accounting_firm_id: str | None = None) -> BusinessRef:
        refs = self.businesses.get(business_id)
        if not refs:
            raise LucaToolError(
                UNKNOWN_BUSINESS,
                f"Business {business_id!r} is not among the businesses you can access.",
                hint=self._access_hint(),
                details={"accessible_business_ids": sorted(self.businesses)[:50]},
            )
        if accounting_firm_id:
            for ref in refs:
                if ref.accounting_firm_id == accounting_firm_id:
                    return ref
            raise LucaToolError(
                UNKNOWN_BUSINESS,
                f"Business {business_id!r} is not linked to firm {accounting_firm_id!r} for you.",
                hint=f"It is available through firm(s): {sorted({r.accounting_firm_id for r in refs})}.",
            )
        if len(refs) == 1:
            return refs[0]
        raise LucaToolError(
            AMBIGUOUS_FIRM,
            f"Business {business_id!r} is linked to several of your firms.",
            hint=f"Pass accounting_firm_id explicitly; options: {sorted({r.accounting_firm_id for r in refs})}.",
        )

    def _access_hint(self) -> str:
        if self.businesses:
            names = ", ".join(
                f"{ref.business_id} ({ref.commercial_name or ref.legal_name})"
                for ref in self.business_list[:8]
            )
            more = (
                "" if len(self.business_list) <= 8 else f" and {len(self.business_list) - 8} more"
            )
            return f"Call list_businesses. You can access: {names}{more}."
        if self.problems:
            return "Luca could not list your businesses: " + "; ".join(self.problems)
        return (
            "You have no businesses with access granted. Ask your firm administrator to grant "
            "access in the Liebre platform."
        )


class TenantCache:
    """Small per-process TTL cache keyed by identity. Bounded; never shared across identities."""

    def __init__(self, ttl_seconds: int, max_entries: int = 500) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._entries: dict[str, tuple[float, TenantContext]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def get(self, key: str) -> TenantContext | None:
        entry = self._entries.get(key)
        if entry and entry[0] > time.time():
            return entry[1]
        self._entries.pop(key, None)
        return None

    def put(self, key: str, ctx: TenantContext) -> None:
        if len(self._entries) >= self._max:
            oldest = min(self._entries, key=lambda k: self._entries[k][0])
            self._entries.pop(oldest, None)
        self._entries[key] = (time.time() + self._ttl, ctx)

    def invalidate(self, key: str) -> None:
        self._entries.pop(key, None)

    def lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())


_cache: TenantCache | None = None


def tenant_cache(settings: Settings) -> TenantCache:
    global _cache
    if _cache is None:
        _cache = TenantCache(settings.tenant_cache_ttl_seconds)
    return _cache


async def load_tenant(
    client: LiebreClient, settings: Settings, *, force: bool = False
) -> TenantContext:
    cache = tenant_cache(settings)
    key = client.identity.cache_key
    if not force:
        cached = cache.get(key)
        if cached:
            return cached
    async with cache.lock(key):
        if not force:
            cached = cache.get(key)
            if cached:
                return cached
        ctx = await _fetch_tenant(client)
        cache.put(key, ctx)
        return ctx


async def _fetch_tenant(client: LiebreClient) -> TenantContext:
    ctx = TenantContext(email=client.email)
    try:
        firms_body = await client.get(
            "/accounting_firms", firm_id=None, what="your accounting firms"
        )
        ctx.firms = list(firms_body.get("data", [])) if isinstance(firms_body, dict) else []
    except LucaToolError as exc:
        if exc.code == "login_expired":
            raise
        ctx.problems.append(f"accounting firms: {exc.message}")
        ctx.firms = []

    for firm in ctx.firms:
        firm_id = firm.get("accounting_firm_id")
        if not firm_id:
            continue
        try:
            body = await client.get(
                "/businesses", firm_id=firm_id, what=f"businesses of firm {firm_id}"
            )
        except LucaToolError as exc:
            if exc.code == "login_expired":
                raise
            ctx.problems.append(f"firm {firm_id}: {exc.message}")
            continue
        for business in body.get("data", []) if isinstance(body, dict) else []:
            ref = BusinessRef(
                business_id=str(business.get("business_id")),
                accounting_firm_id=str(firm_id),
                legal_name=business.get("legal_name"),
                commercial_name=business.get("commercial_name"),
                tax_id=business.get("tax_id"),
                status=business.get("status"),
                favorite=bool(business.get("favorite")),
            )
            ctx.businesses.setdefault(ref.business_id, []).append(ref)
    logger.info(
        "tenant loaded: firms=%s businesses=%s problems=%s",
        len(ctx.firms),
        len(ctx.businesses),
        len(ctx.problems),
    )
    return ctx
