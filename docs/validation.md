# Recruiting demo validation — 2026-09-27

**Status: implementation draft; full acceptance is not complete.**
Baseline main: `42ccdc1`. Branch: `feat/recruitment-demo`.

## Commands and outcomes

| Command | Result |
|---|---|
| `make -C ResolveFlow test` | 311 run: 302 passed, 9 skipped, 0 failures/errors on final run |
| `npm run build --prefix ResolveFlowFrontend` | Passed |
| `PYTHONPATH=. ../.venv/bin/python -m unittest tests.test_portfolio_demo -v` (backend directory) | 6 passed |
| `PYTHONPATH=. ../.venv/bin/python scripts/run_commerce_acceptance.py` (backend directory) | 42 passed |
| `git diff --check` | Passed |

The nine existing opt-in skips are five Postgres POC tests (no AGENT_PG_TEST_DSN)
and four Redis/Chroma dependency tests (RF_DEPENDENCY_TEST not enabled; no isolated
services). They were not deleted or bypassed.

Initial suite run had seven environment errors caused by missing socksio for the
workspace's SOCKS proxy. Installed socksio into the local venv. The next run had
one ONNX embedding model download timeout. After that download completed, the full
suite passed with the nine documented skips. Chroma telemetry emitted nonfatal
PostHog signature errors; they were not suppressed.

## Five scenarios

| Scenario | Actual evidence | Acceptance |
|---|---|---|
| Goods refund policy | API returns sources; no Commerce case | Passed API; lexical policy retrieval, not live hybrid RAG |
| Bare refund request | Eight candidates; no auto-selected case | Passed API |
| Headphones refund | ¥259 quote → confirm → independent approve → receive return → settle → completed; one refund after repeated settle | Passed API; browser pending |
| Basic renewal cancellation | Auto-renew off; same benefits; no refund | Passed API |
| Pro duplicate charge + 401 | ¥99 Commerce quote; explicit technical_agent_offline degradation | Partial: true Technical Agent response not validated |

New tests also cover demo opt-in, distinct JWT subjects, unauthorized user approval,
review before confirmation, per-session object isolation, fresh reset fixtures and
case retrieval. Existing Commerce tests cover stale quotes, partial refunds, own-
identity review rejection and memory/business boundaries.

## Browser and visual acceptance

The supported cloud browser could not open `http://localhost:5173`:
`net::ERR_BLOCKED_BY_CLIENT`. The documented troubleshooting path exposed no local
preview bridge. Browser clicks, console errors, refresh recovery, 1440×900 layout,
1024px layout and screenshot capture therefore remain **unverified**.
API persisted-case retrieval is not claimed as browser refresh proof.
There are no fabricated UI screenshots. See `screenshots/README.md` for the six
required captures. CSS breakpoints are implemented but not visually accepted.

## Remaining work before marking complete

1. Open the running API-backed app in an accessible browser; complete the headphones
   flow, reset, change user, refresh and inspect sources/console.
2. Inspect 1440×900 and 1024px; adjust any overflow or readability problems.
3. Capture all six actual UI screenshots.
4. Configure a real model via server environment and validate the mixed request
   produces both Commerce and Technical Agent results. The offline mode must stay labelled.
5. Review screenshot evidence and remove the draft status only after these pass.

## Delivery scope

UI: scenario strip, conversation, purchase cards, quote/timeline, reviewer view,
preferences and collapsed developer/admin tools. Removed social advertising and
retired task controls from the main experience. Separated ActionCard and advanced
tools from App.vue. Backend: separate opt-in demo host with session-isolated SQLite,
random signing key, expiring user/reviewer JWTs, no admin provisioning; production
application unchanged. Added explicit Pro/Basic aliases to offline intent parsing.

Root/backend/frontend READMEs, architecture SVG, demo script and design audit are
included. Screenshots and live-model/browser acceptance are outstanding.
**Resume readiness: suitable as a work-in-progress repository; not yet verified as
the complete interview-ready demo requested.**
