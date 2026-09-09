# Luca MCP login through the Liebre app — implementation plan

Date: 2026-09-08. Status: proposed, awaiting go.
Supersedes the Google/staff login of ant-rai #1052/#1053 for the `luca-mcp` client.

## 1. Goal

A Liebre user connects Claude Code (or Codex, Cursor) to Luca with **the same login they use
for the Liebre app, and nothing else**. No Google accounts, no second identity, no Auth0 screen
they have never seen. Someone already signed in to the app in that browser finishes the whole
login with one click.

Non-goals for this iteration: acting on Liebre as the user (roles inside a business), the
remote MCP endpoint for claude.ai / Desktop, the "connected devices" management page (listed as
phase 2), enabling JWT signature verification inside the Liebre API (separate PR).

## 2. Why the app hosts the login

| Option | New Auth0 application? | Screens the user sees | Verdict |
|---|---|---|---|
| **A. App-hosted connect page** (this plan) | No | Liebre app login (only if not signed in) + one Liebre-branded confirmation | **Recommended** |
| B. rai redirects to Auth0 Universal Login (the `MCP_OAUTH_IDP=auth0` path merged in #1051) | Yes, a Regular Web Application per environment | Auth0 Universal Login (same tenant, so the password is the same) | Blocked until the application exists; the user still meets a page that is not the app |
| C. Auth0 "Auth for MCP" (agents talk to Auth0 directly) | Yes, plus API configuration | Auth0 consent screen | Not recommended (third-party strict mode, no e-mail claim for DCR clients) |

Facts this rests on (verified in the repos on 2026-09-08):

- The app holds the user's Liebre access token in an httpOnly session and can read it on the
  server: `auth0.getAccessToken()` (`src/lib/auth/server-auth.ts`, `@auth0/nextjs-auth0` ^4.4,
  `audience = AUTH0_AUDIENCE`). The SDK documents this for Server Routes and Server Actions.
- **The app already sends that token to rai**: `LucaApiClient` (`src/service/client/luca-api-client.ts`)
  calls `NEXT_PUBLIC_LUCA_CHAT_API_URL` (rai: `dev-ms-rai-…run.app` / `rai.dev-lk.mx`, stg, prod)
  with `Authorization: Bearer <token>` and `Accounting-Firm-ID`. So "the app hands the user's
  Liebre token to rai" is existing practice, not a new trust relationship.
- rai does **not** verify those tokens today: `src/endpoints/deps.py::RequireAuth` only checks
  that a bearer token is present ("Placeholder behavior"). The Liebre API decodes them with
  `verify_signature=False`. This plan adds the first real verifier (RS256 via the tenant JWKS)
  to rai; the same module can later back `RequireAuth`.
- rai already knows the Liebre Auth0 tenant per environment: `LIEBRE_API_AUTH0_DOMAIN_{DEV,STG,PROD}`
  (token endpoint URL) plus the M2M client id/secret (`src/utils/liebre_api/settings.py`). The
  issuer and JWKS URL derive from it.
- rai already knows the app URL per environment (`LIEBRE_APP_BASE_URL`, defaults
  `https://app-dev.liebre.ai` / `app-stg` / `app`, `src/endpoints/mcp/service.py::platform_root`).
- The Liebre API identifies the user by the `email` claim of the access token
  (`src/auth/validate_user.py`), so the token carries it; M2M tokens have `sub` ending in `@clients`
  and no e-mail.
- rai migrations run automatically on deploy (`docker-entrypoint.sh`, `.gitlab-ci.yml`:
  `alembic upgrade head`); migration files are deliverables, never run by hand against dev.
- The app's middleware redirects any page without a session to `/auth/login` **without**
  `returnTo` (`src/middleware.ts`), so today a deep link lands on `/` after login.

## 3. The flow

```
 Claude Code                luca-mcp (local)             rai (OAuth AS)                 Liebre app (Next.js)             Auth0 (Liebre tenant)
 ───────────                ────────────────             ──────────────                 ────────────────────             ─────────────────────
 login tool ──────────────► PKCE + state,
                            loopback listener
                            opens browser ─────────────► GET /oauth/authorize
                                                         validate client + redirect_uri
                                                         pending row + login-request token (10 min, single use)
                                                         302 ─────────────────────────► GET /luca/connect?request=<token>
                                                                                        middleware: no session?
                                                                                          302 /auth/login?returnTo=/luca/connect?request=… ──► Universal Login (the app's own)
                                                                                          ◄──────────────────────────────────────────────── back with session
                                                         GET /oauth/requests/<token> ◄── page loads request info (client, scopes, expiry)
                                                         ────────────────────────────►
                                                                                        ┌────────────────────────────────────────────┐
                                                                                        │ Conectar Luca con tu asistente de IA        │
                                                                                        │ Vas a conectar como Ana García (ana@…)     │
                                                                                        │ Acceso de solo lectura a tus contabilidades │
                                                                                        │                      [Cancelar] [Conectar] │
                                                                                        └────────────────────────────────────────────┘
                                                         POST /oauth/requests/<token>/approve ◄── route handler, Bearer = user's Liebre access token
                                                         verify RS256 (JWKS), iss, aud, exp, email
                                                         Liebre user_account must be active
                                                         upsert rai user, finalize pending row (auth code)
                                                         200 {status: approved, redirect_to} ──►
                                                                                        303 ─► http://127.0.0.1:<port>/callback?code=…&state=…
                            ◄──────────────────────────── browser hits the loopback listener ◄──
                            POST /oauth/token (code + verifier) ─► tokens (unchanged)
 ◄── "logged in as ana@…"   keychain
```

Cancel: `POST /oauth/requests/<token>/deny` → rai abandons the row → `redirect_to` carries
`error=access_denied&error_description=…&state=…` → the CLI shows the refusal at once (the
`_refuse` behaviour from #1052, now reachable from the app).

Refusal (account not active, e-mail not a Liebre user): same shape, `status: refused`,
`reason`, and a `redirect_to` with the OAuth error; the page shows the reason and still sends
the browser back so the CLI stops waiting.

What the user experiences:

- Already signed in to the app in that browser: browser opens, one page, one click, done.
- Not signed in: the normal Liebre login page, then the same page and click.
- Nothing from Google, nothing from rai, no Auth0 branding beyond the app's own login.

## 4. Work per repository

### 4.1 ant-rai (branch `luca-mcp/login-through-liebre-app`)

**Data model — no migration.** The login-request token *is* the pending row's primary key
(`uuid4`, 122 random bits, generated with `os.urandom`), which rai already creates for every
authorization request since March 2026 and today keeps in its own session cookie. Expiry derives
from the existing `created_at` (+10 minutes); single use from the existing `code IS NULL AND
used = false`. Nothing about Liebre sessions or token lifetimes is copied into rai: the Liebre
access token is verified once at approval and never stored. Net effect on state: one fewer
moving part (the rai session cookie is no longer involved), zero new columns.

**Token verification** — new `src/auth/liebre_token.py`:
`verify_liebre_access_token(token) -> LiebreIdentity(sub, email, name)`.
RS256 with python-jose (already a dependency), keys from
`https://<tenant>/.well-known/jwks.json` (cached, refetched once on unknown `kid`), checks
`iss = https://<tenant>/`, `aud` = the Liebre API identifier, `exp`, non-empty `email`; rejects
M2M subjects (`@clients`) and any `alg` other than RS256. Settings: tenant derived from
`LIEBRE_API_AUTH0_DOMAIN_<ENV>` (override `LIEBRE_AUTH0_ISSUER`), audience from a new
`LIEBRE_AUTH0_AUDIENCE` (same value the app uses as `AUTH0_AUDIENCE`; if rai's machine-token
request already sends an audience, reuse that setting), test seam `LIEBRE_AUTH0_JWKS_URL`.

**Endpoints** (`src/endpoints/oauth/router.py` + `service.py` + `oauth_dao.py`):

- `GET /oauth/authorize` — for `client_id = luca-mcp` when `MCP_LUCA_LOGIN_UI_URL` is set
  (rollout switch; later defaulted from `LIEBRE_APP_BASE_URL` + `/luca/connect`): create the
  pending row with the login token and `302 <ui>?request=<token>`. No rai session cookie is
  involved any more (the approval arrives server-to-server), which also removes the Cloud Run
  cookie dependency. Other clients keep the existing IdP path untouched.
- `GET /oauth/requests/{token}` — `{client_id, client_name, scopes, expires_at, status}` for
  the page. Unknown/expired → 404/410. No user auth (the token is the capability; it reveals only
  the client name and scopes).
- `POST /oauth/requests/{token}/approve` — `Authorization: Bearer <Liebre user access token>`.
  Verify token → `decide_luca_login` (Liebre `user_account` must be `active`) →
  `upsert_user_from_auth0({sub, email, name})` → `finalize_authorization` →
  `200 {status: "approved", redirect_to}`; refusal → `200 {status: "refused", reason, redirect_to}`
  (row abandoned); bad/missing bearer → 401 with `WWW-Authenticate: Bearer`; unknown/expired/used
  request → 404/410/409.
- `POST /oauth/requests/{token}/deny` — abandons the row → `200 {status: "denied", redirect_to}`.
- Token exchange, refresh, revoke, `/api/v1/mcp/*`: unchanged.

**Policy** — `decide_luca_login` loses the `idp` branches: active Liebre user → `user`,
anything else refused with the reason. The Google/staff path (`luca:staff`, `MCP_STAFF_FIRM_IDS`,
`_staff_tenant`, whoami staff variant) is removed from the Luca path in the same PR, since no
login can produce it any more ("No google accounts"). ant-rai #1053 and luca-mcp #5 are closed
without merging.

**Tests** — unit: verifier (good token, wrong `aud`/`iss`, expired, unknown `kid`, M2M subject,
`alg=none`), login-request lifecycle (single use, expiry, deny, refusal shape), authorize redirect,
policy; router tests with a local RSA key pair served by a stub JWKS. E2E: the luca-mcp harness
gains a "play the app" step (GET request info → POST approve with a token minted by the mock IdP,
which learns to serve a JWKS and RS256 access tokens with `email`) so the full client → rai →
Liebre bu-2 path stays proven. `rai-pytest` baseline: no net-new failures. No AI trailers in
commits or the PR.

### 4.2 ant-liebre-app (branch `feat/luca-connect`)

- `src/app/(general)/luca/connect/page.tsx` — server component under the general layout (header
  with the user menu, so "Cerrar sesión" is one click away if it is the wrong account). Reads
  `request` from the query, calls rai `GET /oauth/requests/<token>`, shows the confirmation card
  (Spanish, existing `card`/`button` components). Uses `auth0.getSession()` for name + e-mail and
  `getServerToken()` (not `getServerAuthData`, whose firm lookup redirects users without a firm).
  States: valid request; expired/unknown ("Esta solicitud expiró. Vuelve a ejecutar el inicio de
  sesión desde tu agente."); refused (reason from rai); approved ("Listo. Regresa a tu terminal;
  puedes cerrar esta pestaña." shown by the CLI's own callback page after the redirect).
- `src/app/(general)/luca/connect/approve/route.ts` and `.../cancel/route.ts` — POST handlers
  (plain form posts, no client JS needed): `auth0.getAccessToken()` → rai approve/deny →
  `303 redirect_to`. Same-origin check on `Origin`; the session cookie is SameSite=Lax, so a
  cross-site POST carries no session anyway. Only `http://127.0.0.1|localhost|[::1]:<port>/callback`
  targets are followed (defensive re-check of what rai returns).
- `src/service/luca-connect-service.ts` — the three rai calls, base URL
  `NEXT_PUBLIC_LUCA_CHAT_API_URL` (the rai URL the app already has per environment).
- `src/middleware.ts` — for paths under `/luca/`, redirect to `/auth/login?returnTo=<path+query>`
  so the user comes back to the connect page after logging in (relative `returnTo` only, which is
  all the SDK accepts by default). Making `returnTo` global is a one-line follow-up if the app team
  wants it.
- Tests: vitest for the service and both route handlers (mock `@/lib/auth0` and `fetch`), a story
  for the card. Playwright pass on dev once rai and the page are deployed.
- Phase 2 (separate PR): a "Luca para asistentes de IA" card in `/my-account` with the install
  command and connected devices (rai refresh tokens: created, last used, revoke), backed by two
  small rai endpoints authenticated with the same verifier.

### 4.3 ant-liebre-api

No change is needed for the login: the app already has the token, rai verifies it against the
tenant and checks `user_account` in the Liebre DB (read-only, existing `UserAccessDAO`).
Recommended follow-up PR (independent): enable signature/audience verification in
`src/auth/validate_user.py` with the same JWKS approach, since rai will demonstrate it works with
the real tenant. Acting on Liebre as the user (so business roles apply to Luca calls) remains the
later on-behalf-of item.

### 4.4 luca-mcp

No protocol change: `/oauth/authorize`, `/oauth/token`, refresh and revoke are the same. Work:
README ("login opens the Liebre app"), the harness "play the app" step and mock IdP JWKS/RS256
support, build-log evidence, close PR #5.

## 5. Security notes

- The login-request token is the pending row's random UUID, valid 10 minutes from `created_at`,
  single use, bound to one pending row (client id, redirect URI, PKCE challenge, scope, state).
  Someone who can read rai's database already sees the PKCE challenge and the code hash; the
  token adds nothing to that exposure, so hashing it would buy nothing.
- The authorization code still only ever goes to a loopback `redirect_uri` on the browser's own
  machine and still needs the PKCE verifier, so a request started by someone else cannot deliver
  tokens to them even if a user approves it. The page still says "solo continúa si acabas de
  iniciar este proceso desde tu computadora".
- The user's Liebre access token travels app-server → rai over TLS, exactly as it does for Luca
  chat today; it is never put in a URL or a cookie by this flow and rai does not store it.
- rai verifies signature, issuer, audience and expiry; the Liebre account must be `active`; a
  user removed from Liebre loses Luca at the next token refresh (existing behaviour, refresh
  tokens 30 days, revocable).
- No new Auth0 application, client secret or callback URL.

## 6. Rollout

1. Merge rai (no migration). Nothing changes for users until
   `MCP_LUCA_LOGIN_UI_URL` is set (`https://app-dev.liebre.ai/luca/connect` on dev).
   New dev env: `LIEBRE_AUTH0_AUDIENCE` (value = the app's `AUTH0_AUDIENCE` for dev).
2. Merge and deploy the app page to dev.
3. Set `MCP_LUCA_LOGIN_UI_URL` on dev rai → verify with Claude Code (`claude mcp add luca …`,
   `login`, `whoami`, `list_businesses`), then Codex.
4. Same two variables on stg and prod. Later PR: default the UI URL from `LIEBRE_APP_BASE_URL`
   and delete the Google/Auth0 IdP branches for `luca-mcp`.

Prerequisite for the dev walkthrough: one Liebre **dev** account we can log in with through
the app (a firm admin of `af-2` inviting the tester from firm-settings → user permissions is
enough; that creates the Auth0 user and the `user_account`). Staff without a Liebre account can no
longer use Luca, by design.

## 7. Estimate and order

| Step | Effort |
|---|---|
| rai: verifier, endpoints, policy cleanup, tests, harness E2E (no migration) | ~1 day |
| app: page, handlers, service, middleware `returnTo`, tests | ~½ day |
| luca-mcp docs + evidence, dev walkthrough, PRs | ~½ day |

Order: rai first (the app work needs its endpoints; the harness proves it without the app), app
second, dev walkthrough third.

## 8. What state rai keeps, and why it cannot be less

rai keeps exactly the two tables it has had since March 2026 for its other MCP clients
(`claude-code`): pending authorization requests (minutes) and refresh tokens (30 days,
revocable by `logout`). They are not a copy of anything in the Liebre API or the app: the app's
cookie session belongs to the browser, and the CLI on the user's laptop cannot use it, so
*something* has to issue and be able to revoke the CLI's own credential. The only ways to make
rai fully stateless are (a) a new Auth0 Native application so the CLI holds Auth0 tokens
directly (the blocked option) or (b) handing the CLI a copy of the app's Auth0 refresh token,
which cannot be revoked per device and is the token-sharing pattern the MCP guidance forbids.
Moving the two tables into the Liebre API would relocate the same records, not remove them.

Where the Liebre token is verified is the one open choice:

- **rai verifies it against the tenant JWKS** (recommended): ~80 lines, one new env value
  (`LIEBRE_AUTH0_AUDIENCE`), correct today regardless of the Liebre API's pending TODO.
- **rai delegates to the Liebre API**: rai calls, say, `GET /accounting-firms` with the user's
  token and trusts a 200, then reads `email` from the token. Zero Auth0 configuration in rai,
  but the check is only as strong as the Liebre API's, which does not verify signatures yet.

## 9. Decisions taken in this plan (say so if you want them changed)

- One confirmation click on the connect page rather than auto-connect.
- Google and staff access removed from the Luca login (PRs #1053 / luca-mcp #5 closed).
- The connect page lives under the app's general layout at `/luca/connect`.
- rai keeps the OAuth machinery and the token verification; the Liebre API is untouched.
