# Hostile review — `docs/plans/2026-09-08-luca-mcp-plan.md`

Reviewed 2026-09-08. Every fastmcp claim below was checked by reading
`/Users/anand/scratch/ant-rai/.venv/lib/python3.12/site-packages/fastmcp/` (2.14.5, confirmed via
`fastmcp-2.14.5.dist-info`) and `mcp/server/auth/routes.py` (mcp 1.26.0). Liebre route claims were
checked against `scratchpad/routes.md`. Nothing was run against a live service. Where I could not
verify something I say so.

Severity key: **P0** breaks the beta today or tomorrow, or is factually false. **P1** breaks within
days or is a real security hole. **P2** worth fixing before users arrive.

---

## 1. Blocking flaws

### B1 (P0) — `search_journal_entries` is specified against filters the endpoint does not have

`GET /businesses/{b}/journal_entries/search` accepts exactly `q`, `limit` (default 50), `offset`,
`has_cost_centers`, `tag_id` (routes.md:391). §4.2 of the plan gives the tool
`year_month?, date_from?, date_to?, q?, status?, journal_entry_type?, number?`, and §2.1 asserts the
endpoint "needs at least one filter and caps at 200". None of that is in the route table — every
parameter is optional and there is no declared cap.

The only period-scoped alternative, `GET /businesses/{b}/periods/{period_id}/journal_entries`, takes
`cfdi_details`, `has_cost_centers`, `tag_id`, `tag_ids` — no dates, no status, no type, and **no
`limit`/`offset` at all**. So there is no date-filtered journal-entry query in the API.

This is the tool demo step 6 depends on ("show me the pólizas behind the biggest delta").

Fix: ship `search_journal_entries(business_id, q, limit, offset)` mapped 1:1 to the real endpoint,
plus `list_journal_entries_for_period(business_id, period_id, ...)` that pulls the unpaginated period
list once and filters/pages inside Luca with a hard row cap. Confirm both signatures against
`ant-liebre-api` before writing the handler. Delete the "≥1 filter, max 200" claim from §2.1 and §4.2.

### B2 (P0) — the three report tools omit `cache_key`, and two other parameters are the wrong type

Route table:

| Endpoint | Declared params (routes.md) |
|---|---|
| `reports/trial_balance` | `cache_key:str`, `levels_deep:str='all'`, `start_period_id:str*`, `end_period_id`, `signed_balances`, `include_zero_balances`, `absolute_values`, `only_detail_accounts`, `hide_order_accounts`, `include_pending_entries` |
| `reports/balance_sheet` | `start_period_id:str*`, `end_period_id`, `cache_key:str` |
| `reports/income_statement` | `start_period_id:str*`, `end_period_id`, `cache_key:str`, `cost_center_ids` |

The plan's §4.2 signatures carry no `cache_key`, use `levels_deep=1` as an **int** where the API
declares a **string** (`'all'`), and give `get_balance_sheet` a `period_id` where the API requires
`start_period_id`. `cache_key` is almost certainly the handle that interacts with the
`status: "processing"` behaviour §4.1 plans to poll on; getting it wrong is the most likely way to
produce a report that never resolves during the demo.

Fix: generate the client from the OpenAPI document (§5 already says to) and derive tool signatures
from it rather than the other way round. Establish what `cache_key` means before the demo.

### B3 (P0) — both tenant-discovery calls are gated on firm-admin permissions

`GET /api/v1/accounting_firms` requires `accounting_firm:settings:view`. `GET /api/v1/businesses`
requires `accounting_firm:businesses:view` (routes.md, accounting_firms and businesses sections).
§3.4 builds the entire tenant context from exactly these two calls, and §4.2 makes them the body of
`whoami` and `list_businesses`.

If the demo user is not a firm admin, `whoami` 403s and every business-scoped tool fails, because the
firm header is derived from a map that could not be built. §8 anticipates per-user permission
differences for *reports*; it does not notice that discovery itself is permission-gated.

Fix: today, call both endpoints with the actual demo user's token. If `accounting_firms` 403s, either
have David grant the two view permissions to beta users, or accept an explicit `accounting_firm_id`
argument and skip firm discovery. Add this to §10 Q10.

### B4 (P0) — the demo script needs at least seven distinct Liebre permissions, none of them verified

From routes.md, the demo path touches: `accounting_firm:settings:view`,
`accounting_firm:businesses:view`, `business:periods:view`, `business:reports:trial_balance` (used by
both the trial balance **and** `taxes/vat/determination/{period_id}`),
`business:reports:profit_and_loss`, `business:reports:balance_sheet`, `business:declaraciones:view`,
`business:documents:view` (both `sat_archives` and `documents`),
`business:journal_entries:view_journal_entries`, and `business:settings:coa`.

Verify the demo user holds all of them on the demo business today. This is a fifteen-minute check
that otherwise becomes a dead demo in front of users.

Also note `GET /journal_entries/{id}/review_items` requires
`business:journal_entries:update_journal_entry` — a write permission on a read endpoint. The Phase 1.5
tool `list_journal_entry_review_items` will 403 for read-only users.

### B5 (P0) — the scope design cannot be implemented with `Auth0Provider`

Verified in `providers/auth0.py`: `Auth0Provider.__init__` forwards only `config_url`, `client_id`,
`client_secret`, `audience`, `base_url`, `issuer_url`, `redirect_path`, `required_scopes`,
`allowed_client_redirect_uris`, `client_storage`, `jwt_signing_key`,
`require_authorization_consent`. It does **not** accept `valid_scopes`, `extra_authorize_params`,
`extra_token_params`, `token_endpoint_auth_method`, `forward_pkce`, or `token_verifier`.

`OIDCProxy` accepts the extra params and the auth method, but **also** does not accept `valid_scopes`
(its `init_kwargs` has no such key). Only raw `OAuthProxy` takes it (`oauth_proxy.py:642`).

In `OAuthProxy.__init__`:

```python
client_registration_options = ClientRegistrationOptions(
    enabled=True,
    valid_scopes=valid_scopes or token_verifier.required_scopes,   # :719
)
super().__init__(..., required_scopes=token_verifier.required_scopes)  # :733
```

and `auth.py:475-483` advertises `scopes_supported = valid_scopes or required_scopes`.

So with `Auth0Provider` the advertised `scopes_supported` is exactly `required_scopes`, which
`Auth0Provider` defaults to `["openid"]`. Two consequences:

1. You cannot advertise `luca:read` / `luca:write` / `email` / `offline_access` (§3.2 "Scopes",
   §11.1 `.mcp.json`) except by putting them in `required_scopes` — which also makes `JWTVerifier`
   **require every one of them** in the upstream Auth0 access token's `scope` claim
   (`providers/jwt.py:458-466`). Advertising and enforcing are the same knob.
2. The plan's own escape hatch — "force `openid email offline_access` upstream via
   `extra_authorize_params`/`extra_token_params`" (§3.2) — is unavailable on `Auth0Provider`. It does
   work on `OIDCProxy`: `_build_upstream_authorize_url` sets `scope` and then runs
   `query_params.update(self._extra_authorize_params)` last (`oauth_proxy.py:2085-2103`), so an
   `extra_authorize_params={"scope": ...}` overrides it.

Fix, today: drop `Auth0Provider` and construct `OIDCProxy` (or `OAuthProxy`) directly with
`config_url=https://<tenant>/.well-known/openid-configuration`, `audience=<Liebre API identifier>`,
`required_scopes=["openid"]`, `extra_authorize_params={"scope": "openid email offline_access"}`. If you
want advertised scopes, subclass to pass `valid_scopes`. `Auth0Provider` is a thin wrapper that
removes the four constructor arguments you actually need.

### B6 (P0) — `offline_access` will never be requested, so there will be no refresh token

Claude Code appends `offline_access` only "if advertised by the auth server", and since v2.1.196
requests only the server-provided scopes rather than the full catalogue (cc-guide-brief §1, Scope
Handling). With `scopes_supported = ["openid"]` (B5), Claude Code requests `openid`, Auth0 issues no
refresh token, and every user re-authenticates every 24 hours.

§8 lists this as a tolerable limitation. §3.2 step 9, §6.1's Auth0 ask, and the §11.1 `.mcp.json`
sample all assume refresh works. Pick one today. The `.mcp.json` `oauth.scopes` override does let a
user force scopes (cc-guide: user config overrides server-discovered), but that only helps people who
edit config by hand.

### B7 (P1) — refresh will break even after you make it work

`exchange_refresh_token` sends the **client's stored scopes** upstream, not the pinned authorize
scope:

```python
upstream_scopes = self._prepare_scopes_for_upstream_refresh(scopes)
token_response = await oauth_client.refresh_token(
    url=self._upstream_token_endpoint,
    refresh_token=upstream_token_set.refresh_token,
    scope=" ".join(upstream_scopes) if upstream_scopes else None,
    **self._extra_token_params,
)                                                        # oauth_proxy.py:1464-1472
```

`extra_authorize_params` are authorize-only. So an authorize pinned to `openid email offline_access`
can be followed 24 hours later by a refresh that sends whatever the client registered. Auth0 rejects
scopes outside the original grant, and a narrowed scope yields an access token whose `scope` claim can
fail `required_scopes=["openid"]` — a 401 on day two with no obvious cause.

Good news: `extra_token_params` (which carries `audience` from `OIDCProxy`) **is** forwarded on
refresh, so the Liebre audience survives. That one is fine.

Fix: make the client's registered scope set equal the upstream scope set — advertise exactly
`openid email offline_access` via `valid_scopes`, keep `required_scopes=["openid"]` — so authorize and
refresh send the same string. Test the refresh path before the demo (delete the JTI mapping, or use a
scratch Auth0 API with a short TTL).

### B8 (P0, factually wrong) — the revocation story is false in three places

Claims: §3.1 "can be revoked instantly by deleting its server-side mapping"; §3.2 step 10 "we delete
the mapping and revoke the Auth0 refresh token upstream"; §9 "reference tokens: revocation is
immediate".

`OAuthProxy.revoke_token` does exactly two things: deletes the refresh-token **metadata** row keyed by
hash, and POSTs `{"token": token.token}` to the upstream revocation endpoint. And
`load_refresh_token` returns `RefreshToken(token=refresh_token, ...)` where `refresh_token` is the
**FastMCP JWT** (`oauth_proxy.py:1401-1406`). So:

- Auth0 receives a FastMCP HS256 JWT at its revocation endpoint and revokes nothing.
- `_jti_mapping_store` and `_upstream_token_store` are never touched, so an already-issued Luca access
  token keeps resolving to a live Auth0 token until its own TTL expires.

Separately, the Claude Code docs describe `claude mcp logout` as clearing local credentials; they do
not say it calls the revocation endpoint. Unverified either way.

Fix: override `revoke_token` to delete the JTI mapping and the upstream token set and to POST
`upstream_token_set.refresh_token`. Roughly twenty lines. Do it before the URL goes to users, or stop
claiming instant revocation.

### B9 (P1) — the provider fetches Auth0's discovery document synchronously at construction

`OIDCProxy.__init__` calls `OIDCConfiguration.get_oidc_configuration()`, which is a bare
`httpx.get(config_url)` with `raise_for_status()` and no retry (`oidc_proxy.py`). A slow or
unreachable Auth0 at container start raises and crashloops the revision. With `min=max=1` there is no
second instance to serve while it retries.

Fix: wrap construction in a bounded retry, or build `OAuthProxy` from explicit endpoint URLs baked
into config.

---

## 2. Security holes and spec violations

### S1 (P1) — FastMCP refresh tokens are accepted as access tokens for 30 days

`JWTIssuer.verify_token` validates signature, `exp`, `iss`, `aud` and nothing else (`jwt_issuer.py`).
The refresh token carries `token_use: "refresh"`, but `load_access_token`
(`oauth_proxy.py:1631-1683`) never reads it, and the refresh JTI is written into the same
`_jti_mapping_store` with a 30-day TTL (`:1318-1327`).

So presenting a Luca refresh token as `Authorization: Bearer` authenticates against `/mcp` for the
full refresh lifetime. This contradicts §3.2's "Luca access JWT: mirrors the upstream `expires_in`" and
weakens §9's audience-binding claim.

Fix: subclass and reject `payload.get("token_use") == "refresh"` in `load_access_token`. Three lines.

### S2 (P1) — the redirect-URI allow-list in §3.2 is bypassable as written

`validate_redirect_uri` uses `fnmatch` (`redirect_validation.py`), where `*` matches any characters,
including `/`, `@` and `.`. The plan's pattern `http://localhost:*/callback` therefore matches
`http://localhost:0@evil.example/callback`, whose real host is `evil.example`. Any client that can
register (DCR is open by construction) can exfiltrate an authorization code.

Also worth stating plainly: `allowed_client_redirect_uris=None` means **allow everything** —
"If no patterns specified, allow all for DCR compatibility" (`redirect_validation.py`). The
`Auth0Provider` docstring says "If None (default), all URIs are allowed"; the `OIDCProxy` docstring on
the same parameter says "If None (default), only localhost redirect URIs are allowed". The library's
own docstrings contradict each other; the code allows all. Never ship the default.

Fix: do not use fnmatch for the loopback case. Parse the URL and require scheme `http`, hostname
exactly `localhost`/`127.0.0.1`/`::1`, **empty userinfo**, path exactly `/callback`, any port. Keep
fnmatch only for the fixed https entries.

### S3 (P1) — the RFC 9728 root document is asserted but not produced

§9 requires PRM "at `/.well-known/oauth-protected-resource/mcp` (path-suffixed) **and root**".
`create_protected_resource_routes` (`mcp/server/auth/routes.py:209-253`) returns exactly one route, at
the path-suffixed URL. There is no root document unless you add the route.

Related: §3.5 says `/mcp` works with and without a trailing slash, but `_get_resource_url` produces one
canonical `resource`, and `OAuthProxy.authorize` rejects with `invalid_target` when a client sends an
RFC 8707 `resource` that is not string-equal to it (`oauth_proxy.py:1051-1063`). A user who configures
`.../mcp/` with a client that sends `resource` fails authorization with an opaque error. Publish one
canonical URL and say so in the README.

### S4 (P2, factually wrong) — there is no clock-skew leeway

§8 and §9 promise 60 s leeway. `JWTVerifier.verify_token` compares `exp < time.time()` directly and
never inspects `nbf` or `iat` (`providers/jwt.py:391-397`). No leeway parameter exists. Drop the claim
or write a custom verifier.

### S5 (P2) — RFC 8707 `resource` is forwarded verbatim to Auth0

`_build_upstream_authorize_url` copies the client's `resource` into the Auth0 authorize request
(`oauth_proxy.py:2096-2098`). Auth0 uses `audience`, not `resource`. Probably ignored, but untested,
and it is one more way Universal Login can error during the demo. Do one manual authorize round trip
today.

### S6 (P2) — `LUCA_MCP_STORAGE_KEY` is dead config in the beta

§3.5 says the beta uses "the default encrypted local store". In that path fastmcp derives the storage
key from the JWT signing key and wraps a `DiskStore` in `FernetEncryptionWrapper` itself
(`oauth_proxy.py:813-826`); any key you supply is ignored. `FernetEncryptionWrapper` only becomes your
job when you pass your own store — the plan's §2.2 caveat is correct, but §5 lists the variable as if
it were live today. It is not, until Redis lands in week 1.

### S7 (P2) — consent is a signed browser cookie, not a grant record

Approval is stored per `(client_id, redirect_uri)` in a `samesite=lax`, `path=/`, HMAC-signed cookie
(`_set_list_cookie`). Clearing cookies re-prompts; a shared browser profile carries the approval.
Acceptable, but §9's "decisions logged" is not something fastmcp does — you get a debug log line, not
an audit record. Write your own if you want the checkbox.

### S8 (P2) — every tool handler holds the raw Auth0 token

That is the design and it is the right one, but it means §9's "no token passthrough" is only true in
the client direction. Enforce it: the Liebre client is the single place that reads
`get_access_token().token`, and the outbound host is pinned to `LIEBRE_API_BASE_URL`. Any future tool
that takes a URL argument becomes an SSRF-with-credentials primitive. `get_platform_link` is local
today; keep it that way.

### S9 (P1, product risk) — the beta widens exposure of a known hole

liebre-api does not verify signatures (§2.1, §9, Q11). You are not creating that hole, but you are
adding an internet-reachable path to it, and downstream ms-permissions/ms-cfdis are the only real
gate. Keep Cloud Run ingress restricted and state in the README that the beta is dev-only until Q11
lands.

---

## 3. Missing edge cases (not already in §8)

- **E1 (P0).** Auth0 Actions add custom claims to ID tokens and access tokens through *separate* API
  calls. Your handler reads the decoded **access** token (`AccessToken.claims`,
  `providers/jwt.py:470-475`). If the tenant Action only sets the ID-token claim, `email` is absent
  and §3.2's fail-closed path fires for every user. §10 Q1 asks whether the Action "applies to the new
  client"; the more likely failure is that it applies to the wrong token. Verify with one real token
  today and amend the ask.
- **E2.** If the Auth0 application is not marked first-party, Auth0 shows its own consent screen on
  top of yours. Two consent screens in the demo. Check `is_first_party`.
- **E3.** Refresh-token rotation is a per-application Auth0 setting. §3.2 states "rotated on every
  use" as if it were your behaviour; it is Auth0's, and it may be off. §8 covers reuse detection but
  not "rotation is disabled".
- **E4.** `GET /documents/{document_id}/content` takes `token` as a **query parameter** and has **no
  auth dependency** (routes.md:310). §4.1 says never put tokens in query strings; the API already
  does. Decide explicitly whether `get_document` returns that URL, and never log it.
- **E5.** Nothing establishes the real shape of `period_id`. The route table types it `str`; §4.1
  asserts `YYYYMM` with 13 as annual close. Fetch one real period before writing tools.
- **E6.** Cloud Run concurrency 40 on a single instance means a slow report blocks the OAuth callback
  and consent pages for other users on the same instance. Lower concurrency for the demo or accept it
  knowingly.
- **E7.** `min=max=1` does not guarantee one instance during a revision switch; two revisions serve
  while traffic migrates, and `DiskStore` is per-instance. Do not deploy during the demo window.
- **E8.** The default `DiskStore` writes under `settings.home` (platformdirs). In a non-root container
  with no writable `HOME` this raises on first write, not at boot — so the server starts healthy and
  fails at the first login. Set an explicit writable directory.
- **E9.** Header spelling: rai maps inbound `Accounting-Firm-Id` to upstream `Accounting-Firm-ID`.
  Headers are case-insensitive per RFC, but copy liebre-api's exact spelling anyway.
- **E10.** §3.4 caches the tenant map per instance keyed by `email`. On a shared instance that is a
  cross-user cache. Key it by the token `sub` as well, and never let a cache hit change which firm
  header is sent.
- **E11.** `whoami` reports "token expiry" — say *which* token. The upstream Auth0 `exp` is the one
  that tells a user when they must log in again; the Luca `exp` tells them nothing useful.
- **E12.** `list_businesses` returning zero (new user, or all firms 403) is the most likely first-run
  experience for whoever David adds today. Nothing in the plan says what that response looks like.
- **E13.** §4.1 promises decimals end to end. `structured_content` is JSON; Pydantic serialises
  `Decimal` to a JSON number unless you add a field serialiser. Float round-tripping is exactly the
  bug that makes an accountant distrust the demo.
- **E14.** `get_platform_link` with unconfirmed frontend paths (Q7) produces 404s live. Until the paths
  are known, return the business home only and say the deep link is unavailable.
- **E15.** A 401 caused by a wiped JTI mapping after a redeploy is indistinguishable, to the user, from
  an expired login. Put the build id in the error hint.

---

## 4. Sequencing and timeline

The critical path is Auth0, and putting that ask first is right. The problem is that steps 2-8 all
assume it lands early; if it lands at 16:00 you discover B5, B6 and B7 at 17:00. Reorder:

1. **Send the Auth0 ask**, expanded: access-token `email` claim (E1), first-party flag (E2),
   refresh-token rotation setting (E3), and whether `client_secret_post` is required (C6).
2. **Before any tool code**, get one real token for the demo user and `curl` the five demo calls:
   `accounting_firms`, `businesses`, `reports/trial_balance`, `taxes/vat/determination/{p}`,
   `declaraciones?year=`. Thirty minutes. It settles B1-B4 before you write a handler, and it hands
   you a working `--header` fallback for free.
3. **Six tools, not nineteen**: `whoami`, `list_businesses`, `list_periods`, `get_trial_balance`,
   `get_vat_determination`, `list_declaraciones`. That is the entire demo script except step 6.
4. **Auth** (`OIDCProxy`, not `Auth0Provider`), locally, end to end with Claude Code on localhost.
5. **Cloud Run.**
6. **Then** the remaining thirteen tools, demo-order first.

Cut from today: `get_chart_of_accounts` (largest payload, least demo value), `search_ledger_accounts`,
`get_ledger_account`, `list_documents`, `get_document`, full `extracted_data` in `get_sat_archive`,
`get_platform_link` (paths unknown), and both prompts. Ship them tomorrow afternoon.

Nineteen tools plus a novel auth stack plus a first Cloud Run service in one day, with the auth
dependency owned by someone else, is not a schedule.

Two demo-day risks the plan does not name: with no refresh token (B6) whoever logs in today must
**re-login tomorrow morning**, so rehearse that; and any redeploy tomorrow wipes the `DiskStore` and
logs everyone out, so freeze deployment after the last rehearsal.

---

## 5. Unclear or contradictory

- **C1.** §6.1 step 9 is unparseable: "deploy with a `StaticTokenVerifier`-free path *disabled*, and
  instead run Claude Code with `--header`". A server with `auth=` set rejects a raw Auth0 token; a
  server without `auth=` is unauthenticated on the internet. State it plainly: a second Cloud Run
  service with internal ingress, no `auth=`, tokens supplied by `--header`, torn down after the demo.
- **C2.** §9's "clients never receive Auth0 tokens; Liebre never receives Luca tokens" reads as if two
  token types flow toward Liebre. Only one does.
- **C3.** "revocation is immediate regardless of `exp`" (§3.2) versus §8's "state store lost → every
  user re-logs in once". Both describe the JTI mapping. The first is wrong (B8); the second is right.
- **C4.** Three different statements of the same cap: §2.1 "caps at 200", §4.2 "max 200", §4.1 "hard
  cap 200". The route declares `limit:int=50` and no cap. Do not assert a server-side limit you have
  not seen.
- **C5.** §4.5: "Both use `allowed-tools: mcp__luca__*` and `context: fork` is *not* used (they need
  the conversation)." Double negative, and the wildcard is unverified — the Claude Code skills
  reference documents `Bash(git *)` and literal `mcp__server__tool`, not `mcp__server__*`.
- **C6.** §3.2 says the Auth0 app is confidential with `client_secret_post`. `Auth0Provider` does not
  expose `token_endpoint_auth_method`; `OIDCProxy` does. With `Auth0Provider` you get authlib's default
  (`client_secret_basic`). Either works with Auth0, but do not state a method you cannot set.
- **C7.** §3.5 "SSE responses by default (Claude Code, Codex, Cursor all handle them)" versus §8
  "fastmcp negotiates on `Accept`". The second is right; the first reads like a verified claim about
  three clients when only one was tested.
- **C8.** The localhost callback ask appears twice with two urgencies (§6.1 step 5 and §10 Q1). Merge.
- **C9.** §2.1 says `GET /accounting_firms` takes "no header"; routes.md marks it
  "(firm-header-optional)". Say optional and always send it — one less branch.
- **C10.** §5 lists `AUTH0_DOMAIN` but `OIDCProxy`/`Auth0Provider` need `config_url`, the full
  `.well-known/openid-configuration` URL. Trivial, but it will be the first thing that fails.

---

## 6. Over-engineered for the first two weeks

- **O1.** Nineteen tools on day one when six cover the demo. Every extra tool is a schema §4.1 promises
  to keep "stable forever".
- **O2.** Dual output — `structured_content` *and* a hand-written markdown rendering for every tool.
  Two formatters, two truncation rule sets, double the review surface, for clients you have not
  tested. Structured JSON plus a text summary on the three report tools is enough.
- **O3.** GCS export bucket, 24-hour lifecycle, 15-minute signed URLs, `resource_link`. A bucket, a
  service-account grant, a lifecycle policy and a signing key for a feature nobody has asked for.
- **O4.** Redis token-bucket rate limiting plus a global concurrency cap, for fewer than twenty users.
  A per-instance semaphore until abuse is observed.
- **O5.** Sentry with PII scrubbing, structured logging with a field allow-list, drift logging, and
  latency metrics, all in week 1. Pick structured logs with an allow-list; add Sentry after something
  breaks twice.
- **O6.** `luca-compare-sat` encodes a reconciliation methodology — declared vs ledger, deltas, likely
  causes — before anyone has looked at a single real comparison. Write it after the demo, from what
  David says when he sees the numbers.
- **O7.** Phase 2's `expected_updated_at` optimistic concurrency and the mirrored status-transition
  matrix are correct and belong in week 3.
- **O8.** Companion research notes, a client compatibility matrix, and a plugin marketplace repo. All
  useful, none load-bearing.
- **O9.** `Auth0Provider` itself. It is a wrapper that removes the four constructor arguments you need
  (B5). Use `OIDCProxy` and delete a layer.
