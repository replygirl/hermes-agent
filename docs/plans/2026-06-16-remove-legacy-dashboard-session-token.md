# Remove Legacy Dashboard Session Token — Pluggable Auth As The Only Gate

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Remove the legacy ephemeral `_SESSION_TOKEN` dashboard-auth system entirely, so the pluggable `DashboardAuthProvider` framework is the only *identity* authentication gate. Loopback binds run no identity gate (the bind is the boundary); a shared, credential-free CSRF guard replaces the token's one load-bearing job.

**Architecture:** Today the dashboard runs in exactly one of two mutually-exclusive regimes selected at boot by `should_require_auth(host, allow_public)`: the legacy `_SESSION_TOKEN` (loopback / `--insecure`) or the pluggable OAuth gate (non-loopback). This plan deletes the first regime. The token's only robust contribution on loopback is blocking drive-by CSRF from web pages the user visits; that role moves to a `Sec-Fetch-Site` guard applied uniformly in *both* regimes. Cross-origin reads are already neutralised by the existing `CORSMiddleware` (localhost-only origin regex, `allow_credentials` off). The desktop client — the only external token consumer — migrates to the existing OAuth-cookie/ticket path it already implements for remote gateways.

**Tech Stack:** Python (FastAPI/Starlette middleware in `hermes_cli/web_server.py` + `hermes_cli/dashboard_auth/`), TypeScript SPA (`web/src/lib/api.ts`), Electron desktop (`apps/desktop/electron/*.cjs`), pytest.

---

## Background From The Codebase

Verified against the current worktree (`hermes/hermes-814c0b13`, 2026-06-16). All line numbers are as-found and must be re-confirmed at execution time.

**The two regimes are mutually exclusive and frozen at boot:**
- `should_require_auth(host, allow_public)` (`web_server.py:291`) → `(host not in _LOOPBACK_HOST_VALUES) and (not allow_public)`. Result stashed on `app.state.auth_required` in `start_server` (`web_server.py:10605`).
- `auth_required == False` → legacy token. `auth_required == True` → pluggable gate.

**Legacy token surface (the teardown target):**
- `_SESSION_TOKEN = os.environ.get("HERMES_DASHBOARD_SESSION_TOKEN") or secrets.token_urlsafe(32)` (`web_server.py:185`); `_SESSION_HEADER_NAME = "X-Hermes-Session-Token"` (`:186`).
- `_has_valid_session_token(request)` (`web_server.py:230`) — checks `X-Hermes-Session-Token` or `Bearer`. **2 call sites:** `_require_token` (`:276`) and `auth_middleware` (`:406`).
- `auth_middleware` (`web_server.py:397`) — gates `/api/*` minus `_PUBLIC_API_PATHS`; **already short-circuits when `auth_required`** (`:402`).
- `_require_token(request)` (`web_server.py:250`) — used by **14 sensitive handlers**; already defers to the gate via `request.app.state.auth_required` (`:269`).
- WS auth `_ws_auth_reason` (`web_server.py:9005`) — legacy `?token=<_SESSION_TOKEN>` path (`:9081`), unconditionally rejected in gated mode. Internal `?internal=` credential (`:9051`) and single-use `?ticket=` (`:9065`) are the gated-mode paths.
- SPA bootstrap `_serve_index` (`web_server.py:~9555`) — injects `window.__HERMES_SESSION_TOKEN__` only when NOT `auth_required` (`:9569`); injects `window.__HERMES_AUTH_REQUIRED__` always.
- `_build_gateway_ws_url` (`web_server.py:9157`) — emits `?token=` on loopback, `?internal=` gated.

**Pluggable gate (the keeper):**
- `hermes_cli/dashboard_auth/`: `base.py` (`DashboardAuthProvider` ABC), `registry.py`, `middleware.py` (`gated_auth_middleware`, no-op when not `auth_required`), `cookies.py`, `routes.py`, `ws_tickets.py`, `audit.py`, `prefix.py`, `public_paths.py`.
- Three shipped providers under `plugins/dashboard_auth/`: `nous` (OAuth, bundled default), `self_hosted` (generic OIDC), `basic` (username/password, stateless HMAC, zero external IDP).

**The CSRF/CORS reality (the load-bearing spar result):**
- `CORSMiddleware` (`web_server.py:205`): `allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"`, `allow_methods=["*"]`, `allow_headers=["*"]`, **`allow_credentials` NOT set → defaults False**. So a foreign origin (`evil.com`) can *issue* cross-origin requests but the browser blocks it from *reading* any `/api/*` response body. CORS protects reads, never side effects.
- The token's unique contribution: blocking the **side effects of cross-origin mutations** (a no-preflight "simple" `POST` executes server-side even though CORS hides the reply).
- `Sec-Fetch-Site` is a [forbidden header name](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Sec-Fetch-Site) (browser-asserted, JS cannot forge it), Baseline "widely available" since March 2023 (Chrome 76 / Firefox 90 / Safari 16.4). Electron is pinned `^40.9.3` → Chromium 142–144, always sends it.
- **The SPA addresses its API by host-relative path** (`web/src/lib/api.ts:55`: `fetch(\`${BASE}${url}\`)` where `BASE = window.__HERMES_BASE_PATH__`, a *path* prefix never a host). So SPA→API is **always same-origin in every web deployment** (loopback, dev Vite, reverse-proxy prefix, custom domain). The reverse-proxy-prefix path (`X-Forwarded-Prefix` → `prefix.py`) is a sub-path on one origin, never a host split.
- The packaged desktop renderer loads over `file://` → produces `Sec-Fetch-Site: none` (allowed alongside `same-origin`).

**Nothing outside the dashboard uses session tokens for auth.** Every other `session_token` / `sessionToken` hit in the tree is a name collision: `contextvars.Token` reset handles (`tui_gateway/server.py`, `gateway/platforms/api_server.py`, `gateway/run.py`, `acp_adapter/server.py`) or the Tencent COS upload credential (`gateway/platforms/yuanbao_media.py`). The teardown blast radius is confined to the dashboard + the desktop client.

**Desktop dependency:** `apps/desktop/electron/main.cjs` sets `HERMES_DASHBOARD_SESSION_TOKEN` on the loopback-spawned backend (`:4511`, `:4716`) and sends `X-Hermes-Session-Token` (`:2431`). `connection-config.cjs` already implements both a `'token'` and an `'oauth'` remote-auth model; the remote-OAuth path (`persist:hermes-remote-oauth` cookie + `?ticket=` WS) is the migration target for the local spawn too — or the local backend simply stops needing a credential once loopback has no identity gate.

---

## Key Design Decisions

1. **Loopback = no identity gate.** Per the spar (the user's Q1): nothing off the machine can reach a loopback bind, and the token never actually defended the same-host multi-user case (it is TOFU-readable from `GET /`). So loopback runs *no* identity authentication. "Pluggable auth is the only gate" is satisfied: there is exactly one identity mechanism (pluggable); loopback engages none.
2. **A `Sec-Fetch-Site` CSRF guard replaces the token's CSRF role, in BOTH regimes.** Reject a *present* `Sec-Fetch-Site ∈ {cross-site, same-site}` on state-changing methods; fail-open on absent (non-browser clients, ancient browsers). `{same-origin, none}` pass. This is strictly better than the token (no TOFU secret, browser-asserted) and unifies both regimes under one mechanism.
3. **Scope the CSRF guard to mutating methods** (`POST/PUT/PATCH/DELETE`) + side-effecting routes. Reads are already covered by CORS, so a blanket all-`/api/*` rule adds nothing CORS isn't doing — but extending to GETs is harmless belt-and-suspenders (decided in Open Questions).
4. **No exception list needed.** Every legitimate caller produces `same-origin` (SPA, dev Vite, proxied), `none` (desktop `file://`, user navigation), or no header (curl, NAS probe). A split-host SPA/API topology is unsupported (the relative-fetch design makes it impossible for Hermes' own SPA).
5. **`--insecure` is redefined, not removed.** It currently means "non-loopback bind, no auth at all." Post-change it means "treat this non-loopback bind as loopback-equivalent: no identity gate" — still the trusted-LAN/Tailscale escape hatch, but now ALSO covered by the CSRF guard. The bundled-provider fail-closed check on non-loopback binds is unchanged.
6. **The `?internal=` server-spawned-child credential and the `?ticket=` browser-WS credential both stay.** They are part of the pluggable/gated subsystem, not the legacy token. Only the legacy `?token=<_SESSION_TOKEN>` WS path is removed.
7. **Phase 0 is a regression harness** locking current behavior of BOTH regimes before any teardown, per the infra-change TDD rule.

---

## Open Questions

**Q1 — Loopback identity: no-gate (recommended) vs zero-config local provider. → RESOLVED: Option A (no gate).**
- *Option A (CHOSEN):* loopback runs no identity gate; rely on the OS boundary + CSRF guard + CORS. Matches today's effective security (the token was theater) with less code.
- *Option B (rejected):* ship a zero-config auto-login local `DashboardAuthProvider` so even loopback has a "gate" for uniformity. More code, forces a (silent) session even for single-user local use.
- **Decision: A.** B only earns its place if we later want per-user sessions on a shared local box, which is a different feature (see Q4).

**Q2 — CSRF guard scope: mutations-only (recommended) vs all `/api/*`. → RESOLVED: mutations-only.**
- Mutations-only matches the token's *real* coverage (CORS handles reads). All-`/api/*` is harmless extra defense but may interfere with a future legitimate cross-origin read integration.
- **Decision: mutations-only**, with the guard written so widening to GETs is a one-line change.

**Q3 — Loopback WS auth after `?token=` removal. → RESOLVED: Option (a) Origin/Host guard.**
- Loopback WS (`/api/pty`, `/api/ws`, `/api/pub`, `/api/events`) currently authenticates with `?token=`. With no loopback identity gate, what authenticates the upgrade? Options: (a) rely solely on the existing `_ws_host_origin_is_allowed` Origin/Host guard (loopback bind + same-origin/`file://`/`none` origin); (b) mint a loopback `?ticket=` via the existing ticket store even without an identity gate.
- **Decision: (a)** — the Origin/Host guard is the WS analogue of the CSRF guard, and the bind is the boundary; the `?internal=` child credential is unaffected.

**Q4 — Same-host multi-user isolation. → RESOLVED: explicitly out of scope.** Neither the token nor this plan defends a shared local box where another local user scrapes `GET /` or the cookie. Closing that needs an OS-level mechanism (unix-socket bind + peer-cred check, or a 0600 token file outside served HTML). Park as a separate "multi-user hardening" effort.


---

## Phases Overview

| Phase | What | Lane | Ships independently? |
|---|---|---|---|
| 0 | Regression harness: lock current behavior of BOTH regimes | Teknium-review (auth) | Yes (test-only) |
| 1 | `Sec-Fetch-Site` CSRF guard middleware (additive, both modes) | Teknium-review | Yes |
| 2 | Loopback stops requiring the token (token still injected, inert) | Teknium-review | Yes |
| 3 | Migrate desktop client off `X-Hermes-Session-Token` | Teknium-review (desktop+runtime) | Yes |
| 4 | WS: remove legacy `?token=`; loopback WS via Origin guard | Teknium-review | Yes |
| 5 | Delete `_SESSION_TOKEN` + `HERMES_DASHBOARD_SESSION_TOKEN`; redefine `--insecure` | Teknium-review | Yes |
| 6 | Docs consistency sweep + dead-symbol verification | Docs/mechanical | Yes |

**Lane note:** every code phase touches `hermes_cli/web_server.py` auth paths, `dashboard_auth/`, or the desktop client's runtime auth — all **Teknium-review** territory (general runtime auth semantics), not Ben's Docker self-merge lane. Phase 6 is mechanical/docs.

---

## Phase 0 — Regression Harness (lock current behavior of BOTH regimes)

**Why first:** This is an auth-middleware swap. Per the infra-change TDD rule, Phase 0 builds a harness that pins the *current* observable auth behavior of both regimes against `main` BEFORE any teardown. Every later phase's exit gate is "Phase 0 harness still passes (amended only where the change is intentional)." Run the harness against current `main` and confirm green before touching anything.

**Existing tests to lean on (read first, don't duplicate):** `tests/hermes_cli/test_dashboard_auth_gate.py`, `test_web_server.py`, `test_dashboard_auth_middleware.py`, `test_dashboard_auth_ws_auth.py`, `test_dashboard_auth_401_reauth.py`, `conftest_dashboard_auth.py`. The harness ADDS the behavior-contract tests these don't already cover, it doesn't re-assert them.

### Task 0.1: Pin loopback-mode token enforcement

**Objective:** Lock that, in loopback mode, a non-public `/api/*` route 401s without the token and passes with it.

**Files:**
- Test: `tests/hermes_cli/test_legacy_token_teardown_baseline.py` (create)

**Step 1: Write the test**

```python
import pytest
from fastapi.testclient import TestClient
from hermes_cli import web_server


@pytest.fixture
def loopback_client(monkeypatch):
    # Loopback bind → auth_required False → legacy token regime.
    web_server.app.state.auth_required = False
    web_server.app.state.bound_host = "127.0.0.1"
    web_server.app.state.bound_port = 9119
    return TestClient(web_server.app)


def test_loopback_rejects_without_token(loopback_client):
    r = loopback_client.get("/api/sessions")
    assert r.status_code == 401


def test_loopback_accepts_with_token(loopback_client):
    r = loopback_client.get(
        "/api/sessions",
        headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
    )
    # 200 or 404 (no sessions yet) both prove auth let it through; 401 = fail.
    assert r.status_code != 401


def test_loopback_public_path_needs_no_token(loopback_client):
    assert loopback_client.get("/api/status").status_code == 200
```

**Step 2: Run**

Run: `scripts/run_tests.sh tests/hermes_cli/test_legacy_token_teardown_baseline.py -v`
Expected: PASS (3 passed) against current `main`.

**Step 3: Commit**

```bash
git add tests/hermes_cli/test_legacy_token_teardown_baseline.py
git commit -m "test(dashboard-auth): pin loopback token enforcement baseline"
```

### Task 0.2: Pin that gated mode ignores the legacy token

**Objective:** Lock the mutual-exclusivity invariant: in gated mode the `X-Hermes-Session-Token` header is inert and cookie/gate auth is authoritative.

**Files:**
- Test: `tests/hermes_cli/test_legacy_token_teardown_baseline.py` (extend)

**Step 1: Add tests** (use the existing `conftest_dashboard_auth.py` stub-provider fixtures to register a provider + mint a session cookie; mirror `test_dashboard_auth_gate.py`'s setup).

```python
def test_gated_ignores_legacy_token_header(gated_client):
    # Even WITH a valid legacy token header, gated mode must 401 (no cookie).
    r = gated_client.get(
        "/api/sessions",
        headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
    )
    assert r.status_code == 401
    assert r.json().get("error") in ("unauthenticated", "session_expired")


def test_gated_accepts_session_cookie(gated_client_logged_in):
    r = gated_client_logged_in.get("/api/sessions")
    assert r.status_code != 401
```

**Step 2: Run** → PASS against `main`. **Step 3: Commit** `test(dashboard-auth): pin gated-mode token-inert invariant`.

### Task 0.3: Pin WS auth matrix

**Objective:** Lock the `_ws_auth_reason` contract: loopback accepts `?token=`; gated rejects `?token=`, accepts valid `?ticket=` / `?internal=`.

**Files:**
- Test: `tests/hermes_cli/test_legacy_token_teardown_baseline.py` (extend)

**Step 1: Add unit tests against `_ws_auth_reason` directly** (per the skill's note that `TestClient.websocket_connect` is unreliable for handshake-rejection assertions — test the function, not the socket). Build a fake `ws` with `.query_params` and `.client`.

```python
def _fake_ws(params):
    class _WS:
        query_params = params
        class client: host = "127.0.0.1"
        class url: path = "/api/ws"
    return _WS()


def test_ws_loopback_token_accepted(monkeypatch):
    web_server.app.state.auth_required = False
    reason, cred = web_server._ws_auth_reason(
        _fake_ws({"token": web_server._SESSION_TOKEN})
    )
    assert reason is None and cred == "token"


def test_ws_gated_rejects_token(monkeypatch):
    web_server.app.state.auth_required = True
    reason, cred = web_server._ws_auth_reason(
        _fake_ws({"token": web_server._SESSION_TOKEN})
    )
    assert reason == "no_credential"  # token path not consulted in gated mode
```

**Step 2: Run** → PASS. **Step 3: Commit** `test(dashboard-auth): pin WS auth matrix baseline`.

### Task 0.4: Snapshot the `_require_token` call-site class

**Objective:** Lock the count + identity of `_require_token` call sites as an INVARIANT (not a change-detector) so a later phase can't silently drop a guard.

**Files:**
- Test: `tests/hermes_cli/test_legacy_token_teardown_baseline.py` (extend)

**Step 1: Write a source-introspection test** that greps the module source for `_require_token(request)` call sites (excluding the `def`) and asserts each is also NOT in `PUBLIC_API_PATHS` (the audit invariant from the dashboard skill). Assert `>= 1` and that the set is stable across the teardown — phrased as a relationship, not a hardcoded `== 14`.

```python
import re
from hermes_cli.dashboard_auth.public_paths import PUBLIC_API_PATHS


def test_no_require_token_route_is_public():
    src = (web_server.__file__)
    text = open(src).read()
    # Every _require_token call site must be a gated (non-public) route.
    # We can't easily map call site → path statically, so assert the
    # weaker invariant that PUBLIC_API_PATHS contains no obviously
    # sensitive path, and rely on Task 0.2 for behavior.
    assert "/api/env/reveal" not in PUBLIC_API_PATHS
    assert "/api/providers/validate" not in PUBLIC_API_PATHS
    n_sites = len(re.findall(r"_require_token\(request\)", text)) - 1  # minus def
    assert n_sites >= 1
```

**Step 2: Run** → PASS. **Step 3: Commit** `test(dashboard-auth): pin _require_token gating invariant`.

### Task 0.5: Full-suite green gate

Run: `scripts/run_tests.sh tests/hermes_cli/ -q`
Expected: all pass. This is the baseline every later phase must preserve.

```bash
git add -A && git commit -m "test(dashboard-auth): Phase 0 baseline harness complete"
```

---

## Phase 1 — `Sec-Fetch-Site` CSRF Guard (additive, both modes)

**Why:** This installs the replacement for the token's only load-bearing job (blocking cross-origin mutation side effects) BEFORE the token is removed, so there is never a window with neither defense. The guard is additive and mode-agnostic — it runs in loopback AND gated mode and changes no existing pass/fail for legitimate callers (they all send `same-origin`/`none`/no header).

**Decision (Q2): mutations-only.** Enforce on `POST/PUT/PATCH/DELETE`. Reads are CORS-covered. Written so widening to GETs is a one-line change.

**Middleware ordering (critical):** Per the dashboard skill, FastAPI middleware registration order = runtime order, first-registered runs OUTERMOST. Current order in `web_server.py`: `host_header_middleware` (`:351`) → `_dashboard_auth_gate` (`:390`) → `auth_middleware` (`:396`). The CSRF guard must run AFTER host-header validation (so a bad Host is still rejected first) and can run before or after the auth gate. Place it immediately after `host_header_middleware` and before `_dashboard_auth_gate` so a cross-site mutation is rejected before any auth work.

### Task 1.1: Write the guard's unit tests (TDD)

**Files:**
- Test: `tests/hermes_cli/test_csrf_sec_fetch_guard.py` (create)

**Step 1: Write failing tests**

```python
import pytest
from fastapi.testclient import TestClient
from hermes_cli import web_server


@pytest.fixture
def client():
    web_server.app.state.auth_required = False
    web_server.app.state.bound_host = "127.0.0.1"
    web_server.app.state.bound_port = 9119
    return TestClient(web_server.app)


# A state-changing route that exists and is cheap. Use /api/gateway/restart
# guarded so it doesn't actually restart — or a dedicated test route.
# Prefer asserting the 403 BEFORE auth: send a valid token so only the
# CSRF guard can be the rejecter.
AUTHED = {"X-Hermes-Session-Token": ""}  # filled in fixture


@pytest.mark.parametrize("sfs", ["cross-site", "same-site"])
def test_cross_origin_mutation_blocked(client, sfs):
    r = client.post(
        "/api/providers/validate",
        headers={
            "X-Hermes-Session-Token": web_server._SESSION_TOKEN,
            "Sec-Fetch-Site": sfs,
        },
        json={},
    )
    assert r.status_code == 403
    assert r.json().get("error") == "cross_origin_blocked"


@pytest.mark.parametrize("sfs", ["same-origin", "none"])
def test_same_origin_mutation_allowed(client, sfs):
    r = client.post(
        "/api/providers/validate",
        headers={
            "X-Hermes-Session-Token": web_server._SESSION_TOKEN,
            "Sec-Fetch-Site": sfs,
        },
        json={},
    )
    assert r.status_code != 403  # reaches the handler (400/422/200)


def test_absent_header_fails_open(client):
    # Non-browser client (curl, NAS, desktop): no Sec-Fetch-Site → allowed.
    r = client.post(
        "/api/providers/validate",
        headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
        json={},
    )
    assert r.status_code != 403


def test_cross_site_GET_not_blocked(client):
    # Reads are CORS-covered, not CSRF-guarded (mutations-only scope).
    r = client.get("/api/status", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 200
```

**Step 2: Run → FAIL** (`cross_origin_blocked` doesn't exist yet).
Run: `scripts/run_tests.sh tests/hermes_cli/test_csrf_sec_fetch_guard.py -v`
Expected: the two block tests FAIL (currently 400/422, not 403).

### Task 1.2: Implement the guard middleware

**Files:**
- Modify: `hermes_cli/web_server.py` (insert after `host_header_middleware`, ~line 379)

**Step 1: Add the constant + middleware**

```python
# Methods whose side effects a cross-origin page could trigger without a
# CORS preflight ("simple requests"). Reads are not guarded here — the
# CORSMiddleware (localhost-only origin regex, allow_credentials off)
# already prevents a foreign origin from reading any /api/* response body.
_CSRF_GUARDED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Sec-Fetch-Site values that indicate a same-origin or user-initiated
# request. ``Sec-Fetch-Site`` is a forbidden header name (RFC: browser-set,
# JS cannot forge it), Baseline-available since 2023. ``none`` covers
# user navigation AND the packaged desktop renderer's file:// origin.
_CSRF_SAFE_FETCH_SITES = frozenset({"same-origin", "none"})


@app.middleware("http")
async def csrf_guard_middleware(request: Request, call_next):
    """Reject cross-origin state-changing requests via Sec-Fetch-Site.

    Replaces the legacy _SESSION_TOKEN's only robust contribution: blocking
    drive-by CSRF from a web page the user visits. Applies in BOTH auth
    regimes (loopback and gated). Fail-open on an ABSENT header so
    non-browser clients (curl, the NAS liveness probe, the desktop main
    process) are unaffected — those carry no CSRF risk and the real auth
    gate (cookie / Origin guard) still applies to them.
    """
    if request.method in _CSRF_GUARDED_METHODS and request.url.path.startswith("/api/"):
        sfs = request.headers.get("sec-fetch-site")
        if sfs is not None and sfs not in _CSRF_SAFE_FETCH_SITES:
            return JSONResponse(
                status_code=403,
                content={
                    "error": "cross_origin_blocked",
                    "detail": (
                        "Cross-origin state-changing request rejected. The "
                        "dashboard only accepts mutations from its own origin."
                    ),
                },
            )
    return await call_next(request)
```

**Step 2: Confirm placement** — this block must appear AFTER `host_header_middleware`'s `@app.middleware("http")` and BEFORE `_dashboard_auth_gate`'s, so runtime order is host → csrf → gate → token.

**Step 3: Run → PASS.**
Run: `scripts/run_tests.sh tests/hermes_cli/test_csrf_sec_fetch_guard.py -v`
Expected: all pass.

**Step 4: Run the Phase 0 harness — must still be green.**
Run: `scripts/run_tests.sh tests/hermes_cli/ -q`

**Step 5: Commit**

```bash
git add hermes_cli/web_server.py tests/hermes_cli/test_csrf_sec_fetch_guard.py
git commit -m "feat(dashboard-auth): add Sec-Fetch-Site CSRF guard on mutating /api routes"
```

### Task 1.3: Verify the SPA still passes the guard (behavioral)

**Objective:** Confirm a same-origin SPA mutation carries `Sec-Fetch-Site: same-origin` and is allowed. The browser sets this automatically; no SPA code change is needed. Document the manual check.

**Step 1:** Launch loopback dashboard, open devtools Network tab, trigger any mutation (e.g. save config), confirm the request header `Sec-Fetch-Site: same-origin` is present and the request succeeds. Record in PR description (can't be unit-tested — the browser sets the header, TestClient does not).

**No commit** (verification only).

---

## Phase 2 — Loopback Stops Requiring The Token

**Why:** With the CSRF guard live (Phase 1), the token's protective role on loopback is fully covered. This phase makes loopback `/api/*` accessible WITHOUT the token — but leaves the token still generated/injected (inert) so the SPA and desktop don't break yet. This decouples "stop enforcing" from "delete the symbol," keeping each phase reversible.

**The change:** `auth_middleware` (`web_server.py:397`) currently enforces `_has_valid_session_token` on non-public `/api/*` in loopback mode. After this phase it no longer enforces identity on loopback — the bind is the boundary, CSRF is guarded, CORS covers reads.

### Task 2.1: Flip the loopback enforcement test expectation

**Files:**
- Modify: `tests/hermes_cli/test_legacy_token_teardown_baseline.py`

**Step 1:** Change `test_loopback_rejects_without_token` to assert the NEW behavior: a loopback `/api/*` GET without a token is now ALLOWED (`!= 401`). Rename to `test_loopback_no_identity_gate`. Keep `test_loopback_public_path_needs_no_token`. Add a test that a cross-site mutation is STILL blocked on loopback (the CSRF guard, not identity, is the protection now).

```python
def test_loopback_no_identity_gate(loopback_client):
    # Post-Phase-2: loopback has no identity gate; the bind + CSRF guard
    # + CORS are the boundary. A tokenless read is allowed.
    r = loopback_client.get("/api/sessions")
    assert r.status_code != 401


def test_loopback_still_blocks_cross_site_mutation(loopback_client):
    r = loopback_client.post(
        "/api/providers/validate",
        headers={"Sec-Fetch-Site": "cross-site"},
        json={},
    )
    assert r.status_code == 403
```

**Step 2: Run → FAIL** (loopback still 401s without token).

### Task 2.2: Make `auth_middleware` stop enforcing identity on loopback

**Files:**
- Modify: `hermes_cli/web_server.py:397` (`auth_middleware`)

**Step 1:** The middleware already short-circuits when `auth_required` (gated). Change the loopback branch so it no longer calls `_has_valid_session_token` to gate — loopback `/api/*` is served without an identity check. The simplest correct form makes `auth_middleware` a near no-op (identity enforcement now lives only in the gated path):

```python
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Legacy loopback path: NO identity gate.

    The dashboard's identity authentication is the pluggable gate
    (gated_auth_middleware), engaged only on non-loopback binds. On a
    loopback bind the OS boundary is the security boundary; the
    csrf_guard_middleware blocks cross-origin mutations and CORS blocks
    cross-origin reads. There is no per-request identity token anymore.
    """
    return await call_next(request)
```

> Note: this leaves `auth_middleware` as a no-op shell. Phase 5 removes it entirely (and `_has_valid_session_token`). Keeping it here as a no-op keeps Phase 2's diff minimal and reversible.

**Step 2: Run** the baseline + CSRF suites → PASS.
Run: `scripts/run_tests.sh tests/hermes_cli/test_legacy_token_teardown_baseline.py tests/hermes_cli/test_csrf_sec_fetch_guard.py -v`

**Step 3: Update `_require_token`'s loopback branch.** `_require_token` (`web_server.py:250`) falls back to `_has_valid_session_token` in loopback mode (`:276`). With no loopback identity gate, those 14 handlers must NOT 401 on loopback. Change the loopback branch to `return` (allow) — the CSRF guard already protects them from cross-origin abuse, and they're reads/mutations a local user is entitled to:

```python
    if getattr(request.app.state, "auth_required", False):
        if getattr(request.state, "session", None) is not None:
            return
        raise HTTPException(status_code=401, detail="Unauthorized")
    # Loopback / --insecure: no identity gate. CSRF guard + bind boundary
    # protect these routes; a local user is entitled to call them.
    return
```

**Step 4: Run full hermes_cli suite → green.**

**Step 5: Commit**

```bash
git add hermes_cli/web_server.py tests/hermes_cli/test_legacy_token_teardown_baseline.py
git commit -m "feat(dashboard-auth): drop loopback identity gate (bind+CSRF are the boundary)"
```

---

## Phase 3 — Migrate The Desktop Client Off `X-Hermes-Session-Token`

**Why:** The desktop app is the only external token consumer. It must stop depending on the token before Phase 5 deletes it. Two desktop code paths:
1. **Local spawned backend** (`main.cjs:4511`, `:4716`): sets `HERMES_DASHBOARD_SESSION_TOKEN` on the loopback dashboard it spawns, and sends `X-Hermes-Session-Token` via `fetchJson` (`:2431`).
2. **Remote gateway** (`connection-config.cjs`): already has BOTH a `'token'` and an `'oauth'` model; remote gated gateways use the OAuth cookie + `?ticket=` path.

After Phase 2, the local loopback backend no longer requires the token, so the desktop's local REST calls succeed WITHOUT the header. The migration is: stop sending the header on the local path; keep the remote `'token'` model working only for `--insecure` remote binds (which still have no gate) — but those also no longer check the token after Phase 5, so the header becomes a harmless no-op there too.

**Prerequisites the implementer must verify:** Phase 2 is merged; a loopback dashboard serves `/api/*` without a token (curl check). If false, STOP.

### Task 3.1: Stop minting/sending the token on the local spawn path

**Files:**
- Modify: `apps/desktop/electron/main.cjs` (`:4511`, `:4716` — drop `HERMES_DASHBOARD_SESSION_TOKEN`; `:2431` — drop the `X-Hermes-Session-Token` header for local connections)
- Test: `apps/desktop/electron/connection-config.test.cjs` (extend)

**Step 1:** Trace the `token` variable feeding `:4511`/`:4716`/`:2431`. For the LOCAL spawned backend, stop setting `HERMES_DASHBOARD_SESSION_TOKEN` in the child env and stop attaching the header in `fetchJson`. The local backend now authenticates by nothing (loopback, no gate) — same as a browser hitting `127.0.0.1`.

**Step 2:** For the WS path (local PTY/gateway WS), confirm the desktop uses the same Origin-allowed loopback path the SPA uses. The desktop renderer is `file://` → `Sec-Fetch-Site: none` and the Origin guard accepts `file://` on loopback. No ticket needed on loopback (Phase 4 confirms).

**Step 3:** Keep `fetchPublicJson` (`:2482`) unchanged — it already sends no token.

**Step 4: Run desktop unit tests.**
Run (from `apps/desktop`): `NODE_ENV=development npm test` (or the repo's configured desktop test command — verify in `apps/desktop/package.json`).

**Step 5: Commit**

```bash
git add apps/desktop/electron/main.cjs apps/desktop/electron/connection-config.test.cjs
git commit -m "feat(desktop): drop X-Hermes-Session-Token on local backend (no loopback gate)"
```

### Task 3.2: Behavioral verification — desktop drives a local backend tokenless

**Objective:** Launch the desktop app against a freshly-spawned local backend and confirm REST + WS (chat) work with no session token in the env or headers.

**Step 1:** `NODE_ENV=development npm ci --include=dev` + `env -u NODE_ENV npm run <dev-launch>` (per memory: Ben's host needs `HERMES_DESKTOP_DISABLE_GPU=1`; screenshots are blocked on Wayland — prove via `~/.hermes/logs/desktop.log` + a REST curl, and ask Ben to eyeball the chat tab).
**Step 2:** Confirm `desktop.log` shows successful `/api/status`, `/api/sessions`, and a working `/chat` PTY with no `X-Hermes-Session-Token` and no `HERMES_DASHBOARD_SESSION_TOKEN`.

**No commit** (verification only — record in PR).

---

## Phase 4 — WS: Remove Legacy `?token=`; Loopback WS Via Origin Guard

**Why:** The WS upgrade path still accepts `?token=<_SESSION_TOKEN>` on loopback (`_ws_auth_reason`, `web_server.py:9081`). Per Q3 (recommended: Origin guard), loopback WS no longer needs a token — `_ws_host_origin_is_allowed` (the WS analogue of host_header + CSRF) is the boundary on a loopback bind, exactly as the bind is the boundary for HTTP. The gated `?ticket=` and the server-spawned `?internal=` credentials are UNTOUCHED (they belong to the pluggable subsystem).

**The four WS endpoints:** `/api/pty`, `/api/ws`, `/api/pub`, `/api/events` — all go through `_ws_auth_ok` → `_ws_auth_reason`.

### Task 4.1: Update the WS auth matrix tests

**Files:**
- Modify: `tests/hermes_cli/test_legacy_token_teardown_baseline.py`

**Step 1:** Replace `test_ws_loopback_token_accepted` with the new contract: in loopback mode `_ws_auth_reason` no longer requires a token — the upgrade is allowed when the Origin/Host guard passes. Since `_ws_auth_reason` won't be the layer doing Origin checks, the cleanest post-change contract is: loopback returns `(None, "loopback")` regardless of token, and gated is unchanged (`?ticket=`/`?internal=` only). Keep `test_ws_gated_rejects_token`.

```python
def test_ws_loopback_no_token_required(monkeypatch):
    web_server.app.state.auth_required = False
    reason, cred = web_server._ws_auth_reason(_fake_ws({}))
    assert reason is None  # loopback: Origin guard is the boundary, no token


def test_ws_gated_still_requires_ticket_or_internal(monkeypatch):
    web_server.app.state.auth_required = True
    reason, _ = web_server._ws_auth_reason(_fake_ws({}))
    assert reason == "no_credential"
```

**Step 2: Run → FAIL.**

### Task 4.2: Drop the loopback token branch in `_ws_auth_reason`

**Files:**
- Modify: `hermes_cli/web_server.py:9081-9086`

**Step 1:** Replace the trailing loopback token block:

```python
    # Loopback / --insecure: no per-connection identity token. The WS
    # Origin/Host guard (_ws_host_origin_is_allowed) is the boundary here,
    # mirroring the HTTP-side bind boundary + CSRF guard. Gated mode handled
    # above via ?ticket= / ?internal=.
    return None, "loopback"
```

**Step 2:** Confirm `_ws_host_origin_is_allowed` is actually invoked on the loopback WS path. Trace each of the four WS handlers — they should call BOTH the origin guard and `_ws_auth_ok`. If any handler relied ONLY on `_ws_auth_ok`'s token check for loopback protection, add the origin guard call (it likely already runs — verify, don't assume).

**Step 3: Run** WS tests + full suite → green.
Run: `scripts/run_tests.sh tests/hermes_cli/ tests/docker/test_dashboard.py -q`

**Step 4: Commit**

```bash
git add hermes_cli/web_server.py tests/hermes_cli/test_legacy_token_teardown_baseline.py
git commit -m "feat(dashboard-auth): loopback WS via Origin guard, drop legacy ?token="
```

---

## Phase 5 — Delete `_SESSION_TOKEN`; Redefine `--insecure`

**Why:** Everything now works without the token. This phase removes the dead symbols so no future code can lean on them. After this phase, `grep _SESSION_TOKEN hermes_cli/web_server.py` returns only the deletion's absence.

### Task 5.1: Remove the token symbols + SPA injection

**Files:**
- Modify: `hermes_cli/web_server.py`
- Modify: `web/src/lib/api.ts`

**Step 1 (server):** Delete in `web_server.py`:
- `_SESSION_TOKEN` + `_SESSION_HEADER_NAME` (`:185-186`)
- `_has_valid_session_token` (`:230-247`)
- the no-op `auth_middleware` shell (Phase 2 left it) — remove the `@app.middleware` entirely
- `_require_token`'s loopback `_has_valid_session_token` reference is already gone (Phase 2); keep the function (the gated branch still guards 14 routes), just ensure the loopback branch is a bare `return`
- `_serve_index`: remove the `window.__HERMES_SESSION_TOKEN__` injection branch (`:9568-9574`); keep `__HERMES_AUTH_REQUIRED__` + `__HERMES_BASE_PATH__`
- `_build_gateway_ws_url`: remove the `?token=` loopback branch; loopback emits a bare ws URL (Origin guard authenticates)
- `start_server`: stop honoring `HERMES_DASHBOARD_SESSION_TOKEN` env (it fed `_SESSION_TOKEN`)

**Step 2 (SPA):** In `web/src/lib/api.ts`:
- remove `window.__HERMES_SESSION_TOKEN__` read (`:51`) + `setSessionHeader` call (`:53`) + the `__HERMES_SESSION_TOKEN__` global decl (`:26`)
- keep `credentials: 'include'` (gated cookie path) and `HERMES_BASE_PATH`
- the WS auth-param builder (`buildWsAuthParam`) keeps its gated `?ticket=` path; the loopback branch returns no token param (bare URL)

**Step 3:** Update the plugin SDK contract (`web/src/plugins/sdk.d.ts`) + the bundled plugins (`plugins/kanban/dashboard/dist/index.js`, `plugins/hermes-achievements/dashboard/dist/index.js`) if any still read `__HERMES_SESSION_TOKEN__` directly. Per the dashboard skill, the C-guard test `tests/plugins/test_plugin_dashboard_auth_contract.py` already fails if a plugin reads the token directly — run it; if it passes, plugins are clean.

**Step 4: Run the full suite.**
Run: `scripts/run_tests.sh tests/hermes_cli/ tests/plugins/ tests/docker/test_dashboard.py -q`

**Step 5: Build the SPA** to confirm no TS references dangle.
Run (from `web/`): `npm run build`
Expected: no `__HERMES_SESSION_TOKEN__` / `setSessionHeader` reference errors.

**Step 6: Commit**

```bash
git add hermes_cli/web_server.py web/src/lib/api.ts web/src/plugins/sdk.d.ts
git commit -m "refactor(dashboard-auth): delete legacy _SESSION_TOKEN and SPA injection"
```

### Task 5.2: Redefine `--insecure`

**Files:**
- Modify: `hermes_cli/web_server.py` (`start_server` `:10592`, `should_require_auth` docstring `:291`)
- Modify: `hermes_cli/main.py` (`--insecure` help text near the dashboard parser)

**Step 1:** `should_require_auth` is unchanged in logic (`--insecure` still → no gate) but its docstring must now say "no IDENTITY gate; the CSRF guard + Origin guard still apply." Update the `start_server` `--insecure` warning (`:10660`) to: "Binding to %s with --insecure — no identity authentication. The Sec-Fetch-Site CSRF guard and WS Origin guard still apply; rely on network controls for confidentiality."

**Step 2:** Update `main.py`'s `--insecure` help to match (no longer "no authentication" — "no identity gate").

**Step 3: Run** `scripts/run_tests.sh tests/hermes_cli/ -q` → green.

**Step 4: Commit**

```bash
git add hermes_cli/web_server.py hermes_cli/main.py
git commit -m "refactor(dashboard-auth): redefine --insecure as no-identity-gate (CSRF/Origin still apply)"
```

### Task 5.3: Dead-symbol sweep

**Step 1:** Confirm zero dangling references:
```bash
grep -rn "_SESSION_TOKEN\|_has_valid_session_token\|HERMES_DASHBOARD_SESSION_TOKEN\|__HERMES_SESSION_TOKEN__\|X-Hermes-Session-Token\|setSessionHeader" \
  hermes_cli/ web/src/ apps/desktop/ plugins/ --include="*.py" --include="*.ts" --include="*.tsx" --include="*.cjs" \
  | grep -v "tests/\|/dist/"
```
Expected: empty (or only doc/comment references slated for Phase 6). Any remaining production reference is a missed teardown — fix before proceeding.

**Step 2: Commit** any stragglers found.

---

## Phase 6 — Docs Consistency Sweep + Dead-Symbol Verification

**Why:** User-facing docs and the `environment-variables.md` reference still describe the session token. Per the docs-consistency rule, apply the terminology change across ALL affected pages.

### Task 6.1: Sweep docs

**Files (from the initial grep):**
- `website/docs/user-guide/features/web-dashboard.md`
- `website/docs/user-guide/desktop.md`
- `website/docs/reference/environment-variables.md` (remove `HERMES_DASHBOARD_SESSION_TOKEN`)
- `website/docs/reference/faq.md`
- `website/docs/user-guide/docker.md`
- `apps/desktop/src/i18n/en.ts` (any "session token" Settings-field strings + the gated-mode hint)
- the zh-Hans mirrors of each

**Step 1:** Replace "session token" auth descriptions with: loopback = no identity gate (bind boundary + CSRF guard); non-loopback = pluggable provider login. Remove the desktop "session token" Settings field doc if Phase 3 removed the field. Strip `HERMES_DASHBOARD_SESSION_TOKEN` from the env-var reference.

**Step 2:** Re-grep the docs tree for "session token" / "session-token" / `HERMES_DASHBOARD_SESSION_TOKEN` to confirm none remain outside historical changelogs.

**Step 3: Commit**

```bash
git add website/docs/ apps/desktop/src/i18n/
git commit -m "docs(dashboard-auth): remove legacy session-token references"
```

### Task 6.2: Update the hermes-dashboard skill

**Step 1:** The `hermes-dashboard` skill's "Current auth model" + "two auth paths are MUTUALLY EXCLUSIVE" sections describe the token regime as live. After this lands, patch the skill (via `skill_manage`) to: loopback = no identity gate + CSRF guard; the token is removed; the mutual-exclusivity table collapses to "gated vs no-identity-gate." Do this as the final step so the skill reflects shipped reality.

**No repo commit** (skill lives in `~/.hermes/skills/`).

---

## Risk Register

| # | Risk | Likelihood | Blast radius | Mitigation |
|---|---|---|---|---|
| R1 | A browser/proxy strips `Sec-Fetch-Site`, weakening CSRF defense | Low (Baseline 2023; proxies rarely strip `Sec-`) | A cross-origin mutation could land on loopback | Fail-open is intentional for non-browsers; the *attacker's* browser DOES send it, so a real drive-by is still caught. Residual = a proxy that strips it AND a user behind it — narrow. Document in the warning. |
| R2 | A legitimate caller sends `cross-site`/`same-site` and gets 403 | Very low | That caller's mutations break | Verified: SPA fetches host-relative (`same-origin`), desktop is `file://` (`none`), non-browsers send nothing. No supported topology produces `cross-site` to its own API. Phase 1 Task 1.3 confirms behaviorally. |
| R3 | A split-host SPA/API proxy deployment (unsupported) breaks | Very low | That operator's dashboard | Unsupported by design (relative-fetch). Call out explicitly in docs (Phase 6). The `X-Forwarded-Prefix` mechanism is sub-path-on-one-origin only. |
| R4 | Same-host multi-user box: another local user hits the tokenless dashboard | Existing (token never defended it) | Local config/key access | NOT a regression — the token was TOFU-readable. Parked as Q4 "multi-user hardening" (unix-socket + peer-cred). State in docs that loopback assumes a single-user host. |
| R5 | Desktop client breaks if Phase 3 lands before Phase 2 deploys | Medium if mis-sequenced | Desktop can't reach local backend | Phase 3 prerequisites block explicitly require Phase 2 merged + curl-verified. Phases ship in order. |
| R6 | A `_require_token` route becomes unreachable or over-exposed | Low | One of 14 sensitive routes | Phase 0 Task 0.4 invariant + Phase 2 keeps the gated branch intact; the loopback branch allowing is correct (local user entitled). Gated mode unchanged. |
| R7 | Bundled plugins still read `__HERMES_SESSION_TOKEN__` | Low | Plugin REST 401s | The C-guard test (`test_plugin_dashboard_auth_contract.py`) already enforces SDK use; Phase 5 Task 5.1 Step 3 runs it. |

## Rollout

- **Each phase is independently shippable and reversible.** Phases 0–1 are pure additions (harness + guard) with zero behavior change for existing users. Phase 2 is the first behavior change (loopback no longer needs the token) but the token is still injected so nothing breaks. Phases 3–4 migrate consumers. Phase 5 deletes. Phase 6 is docs.
- **No release-tag retagging** (per Ben's fix-forward policy): this all lands on `main`.
- **Suggested PR grouping:** Phases 0+1 as one PR (harness + guard, additive, low-risk, easy review). Phase 2 alone (the semantic change — most scrutiny). Phase 3 alone (desktop). Phases 4+5 together (WS + delete). Phase 6 alone (docs). Each is Teknium-review except Phase 6.
- **Rollback:** revert the offending phase's PR; because the token symbol survives until Phase 5, reverting Phases 2–4 restores full token enforcement cleanly. After Phase 5 a rollback is a larger revert — gate Phase 5 on Phases 2–4 having been live on `main` without incident.

## Verification (end-to-end, post-Phase-5)

1. **Loopback, no token:** `curl http://127.0.0.1:9119/api/sessions` → 200/404 (not 401). Browser dashboard works with no `X-Hermes-Session-Token` header anywhere in the Network tab.
2. **Loopback CSRF:** `curl -X POST -H 'Sec-Fetch-Site: cross-site' http://127.0.0.1:9119/api/providers/validate` → 403 `cross_origin_blocked`. Same POST with no `Sec-Fetch-Site` → reaches handler.
3. **Gated unchanged:** non-loopback bind with the `nous` (or `basic`) provider → `/api/sessions` 401 without cookie, 200 with cookie; login flow works; refresh rotation works (basic provider is the cheapest to test per the dashboard skill's basic-auth path).
4. **Desktop:** local app launches, chat + REST work tokenless (Phase 3 Task 3.2 evidence).
5. **WS:** loopback `/chat` PTY connects with no `?token=`; gated `/api/ws` still requires `?ticket=`/`?internal=`.
6. **Dead-symbol grep** (Phase 5 Task 5.3) returns empty outside tests/dist.
7. **Full suite:** `scripts/run_tests.sh tests/ -q` green.

## Timeline (rough, person-days)

| Phase | Est. | Lane |
|---|---|---|
| 0 | 0.5 | Teknium-review |
| 1 | 0.5 | Teknium-review |
| 2 | 1.0 | Teknium-review |
| 3 | 1.0 | Teknium-review (desktop) |
| 4 | 0.5 | Teknium-review |
| 5 | 0.5 | Teknium-review |
| 6 | 0.5 | Docs/mechanical |
| **Total** | **~4.5 days** | |

The pluggable gate itself needs **no new work** — it's already mature (three providers, refresh tokens, WS tickets, internal credentials all shipped). This plan is purely teardown + the CSRF-guard replacement.





---

## Appendix A — Documentation Impact (answer to "what docs need updating?")

Phase 6 owns the doc sweep. The complete list of touch points, verified against the repo grep:

**User-facing docs (`website/docs/`):**
- `user-guide/features/web-dashboard.md` — has an "OAuth Authentication (gated mode)" section AND describes the loopback session-token model. Rewrite the loopback half to "no identity gate (the loopback bind is the boundary) + a `Sec-Fetch-Site` CSRF guard."
- `user-guide/desktop.md` — describes the desktop "session token" Settings field + the SSH-tunnel remote workaround. Update for the tokenless local path.
- `reference/environment-variables.md` — remove the `HERMES_DASHBOARD_SESSION_TOKEN` entry.
- `reference/faq.md` — any "how is the dashboard secured?" entry.
- `user-guide/docker.md` — references session-token auth for the containerized dashboard.
- The `i18n/zh-Hans/...` mirror of each of the above (per the docs-consistency-across-all-pages rule).

**In-app strings (`apps/desktop/src/i18n/en.ts`):** the "session token" Settings-field label/help + the gated-mode hint. (zh mirror too if present.)

**Skill (not a repo file):** `hermes-dashboard` skill's "Current auth model" + "two auth paths are MUTUALLY EXCLUSIVE" sections — Phase 6 Task 6.2.

**Verification:** after the sweep, `grep -ri "session token\|session-token\|HERMES_DASHBOARD_SESSION_TOKEN" website/docs/ apps/desktop/src/` returns only historical changelog entries, nothing descriptive of current behavior.

---

## Appendix B — Follow-On (NOT part of this plan): fully removing `--insecure`

Scoping-only analysis for a *future* effort. This plan keeps `--insecure` (redefined as "no identity gate; CSRF + Origin guards still apply"). Removing the flag entirely is a separate decision with its own tradeoffs.

**What `--insecure` does after this plan lands:** it lets a NON-loopback bind run with no identity gate (the gate would otherwise engage and fail-closed without a provider). It's the trusted-LAN / Tailscale / reverse-proxy-with-its-own-auth escape hatch. It no longer disables anything else — CORS, the CSRF guard, and the WS Origin guard all still apply.

**What removing it would require:**
1. **A replacement for the legitimate use cases.** Today `--insecure` serves: (a) trusted-LAN binds where the operator owns the network; (b) deploys behind a reverse proxy that does its own auth (so the dashboard's gate is redundant); (c) local testing of a non-loopback bind. Each needs a sanctioned path before the flag can go — most likely "register a `DashboardAuthProvider` (even `basic`)" for (a)/(c), and a documented "trust the proxy" provider or an explicit `dashboard.trusted_proxy_auth` config for (b).
2. **Make the gate the only non-loopback path.** `should_require_auth` would drop the `allow_public` term entirely: non-loopback ⇒ gate always engages ⇒ a provider is mandatory. The `start_server` fail-closed branch already exists; it would become unconditional for non-loopback.
3. **Migrate every existing `--insecure` operator.** Breaking change — anyone scripting `hermes dashboard --host 0.0.0.0 --insecure` would need to configure a provider first. Needs a deprecation cycle: warn on `--insecure` for one release, then remove.
4. **The `basic` provider makes this viable.** Because `plugins/dashboard_auth/basic/` is zero-infrastructure (username/password, no external IDP), "configure a provider" is now a low bar — the main argument against removing `--insecure` (forcing OAuth on a LAN box) is already answered. This is the strongest reason the follow-on is feasible.

**Rough effort:** 1–2 days + a one-release deprecation window. **Lane:** Teknium-review (changes `should_require_auth` semantics). **Recommendation:** viable as a follow-up once this plan ships and the `basic` provider is the documented LAN path; not urgent, and explicitly out of scope here.

---

## Implementation reality vs plan (June 2026 — as executed)

Banked here so the next reader sees where execution diverged from the plan as written.

- **Phases 4 and 3 were SWAPPED.** The plan ordered Phase 3 (desktop off the token) before Phase 4 (server stops requiring the WS `?token=`). But the desktop's LOCAL chat WS authenticates with `?token=<minted>`, and the server's loopback WS *required* a matching token until Phase 4 landed — so doing Phase 3 first would have broken local desktop chat in the interim. Executed order: 0 → 1 → 2 → **4 → 3** → 5 → 6. Guard-before-teardown applies to inter-phase ordering, not just within a phase.
- **Phase 2 test fallout was ~30 tests across 5 files**, not the 3 the plan named (behavior-change phases under-count fallout — the broad suite is the real enumerator). Sensitive endpoints (`/api/env/reveal`, `/api/fs/*`, admin) gained gated-mode coverage rather than losing their auth assertions.
- **Phase 3 deleted `apps/desktop/electron/dashboard-token.cjs` + its test entirely** (it existed only to reconcile the served `__HERMES_SESSION_TOKEN__` for the local backend). The desktop's REMOTE 'token' auth mode (self-hosted remote gateways) was KEPT — it's a separate, still-valid feature.
- **Phase 6 desktop i18n was NOT touched.** Appendix A assumed Phase 3 removed a desktop "session token" Settings field. It didn't — those i18n strings describe the kept REMOTE-gateway token mode, so they stay. The docs sweep also found `environment-variables.md` had no `HERMES_DASHBOARD_SESSION_TOKEN` entry to remove (drift), and several `website/docs` "session token" hits were false positives (basic-auth cookies, LLM `/usage`, HA tokens) left untouched.
- **The middleware order in the plan/skill was BACKWARDS.** Verified runtime order (outermost→innermost): `auth → gate → csrf → host → CORS` (Starlette prepends; last-registered = outermost). The CSRF guard's real job is blocking *authenticated* cross-site mutations; unauthenticated gated ones are caught by the outer gate first.
- **Two subagent delegations for mechanical test/SPA fixing TIMED OUT** (600s) when told to "verify" — they ran the broad suite repeatedly. Fix: forbid broad-suite runs in the delegation prompt; mandate per-file (`pytest <one_file>`) / `tsc --noEmit` verification. A timed-out subagent still leaves usable partial edits — salvage-and-verify (`git status`, grep for dangling deleted-symbol refs, run the affected files) rather than discarding.