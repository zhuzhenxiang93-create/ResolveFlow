# Product Evaluation validation — 2026-10-05

**Scope:** new Product Evaluation for the recruiting-demo Commerce chain, plus fixes
driven by its Bad Case analysis. Base commit `822ff13` (origin/main). Run in a Linux
cloud sandbox with Python 3.11 (`uv` venv from `ResolveFlow/requirements.txt`) and
Node 22. **No model provider was reachable from this sandbox** (DashScope blocked by
the egress policy), so every Product Evaluation number below is **offline-rules
mode**. `--live` is implemented but has not been run yet.

| Command | Result |
|---|---|
| `PYTHONPATH=. python -m unittest discover -s tests` (before changes) | 313 run: 299 passed, 9 skipped, **5 errors** |
| same, after changes | 328 run (15 new): **314 passed, 9 skipped, 5 errors** |
| `scripts/run_commerce_acceptance.py` | **42/42 passed** before and after |
| `python -m unittest tests.test_portfolio_demo` | 8/8 passed |
| `python -m unittest tests.test_product_eval` | 15/15 passed |
| `scripts/run_product_eval.py --label baseline --save-baseline` | 48/80 passed (first run, kept) |
| `scripts/run_product_eval.py --dataset holdout --label baseline --save-baseline` | 2/16 passed (written before any fix) |
| `scripts/run_product_eval.py --label after_fix` / `--dataset holdout` | 80/80 · 15/16 |
| `npm run build --prefix ResolveFlowFrontend` | Passed |
| `git diff --check` | Passed |

The 5 errors are the same before and after the changes. In each one, Chroma tries to
download its ONNX embedding model and the sandbox proxy returns HTTP 403
(`test_commerce_knowledge` ×1, `test_hybrid_retriever` ×1,
`test_subscription_rag_scope` ×3). The 2026-09-27 macOS run below passed these
tests with a cached model. Re-run `make test` locally to confirm before quoting new
totals. The 9 skips are the same opt-in Postgres/Redis/Chroma tests.

The browser check used Playwright/Chromium against Vite + the isolated Demo host, at
1440×900 and 390×844. The Evaluation view loads from `/eval/product/latest` with
0 console errors or warnings, and has no horizontal overflow on mobile. Clicking a
bad case loads `/eval/product/cases/{id}`. On the Demo view, the Billing + technical
scenario still shows the ¥99 quote. In offline mode, 401 is now explained by the
read-only `lookup_error_code` table and labelled “Offline read-only tool · not a
model diagnosis”.

Regressions checked: refund confirmation → independent review → return → settlement;
user/reviewer separation; cross-session isolation; idempotent double clicks; stale
quotes; Reset Demo. These are covered by the unchanged portfolio/commerce tests and by
the authorization, confirmation and idempotency cases of the evaluation itself.
Refresh restore was not re-checked in a browser this time; the Case API it relies on
is unchanged.

What the numbers mean and do not mean: see [product-evaluation.md](product-evaluation.md).
The fixes were derived from the 80-case failures, so the 100% re-run overstates
generalisation. The 16-case holdout (93.8%) is the fairer signal. It is still a
small, self-authored set on simulated data.

---

# Recruiting demo validation — 2026-09-27

**Status: local recruiting-demo acceptance passed.** This is a simulated local
portfolio application, not production payment processing or a hybrid-RAG benchmark.
Branch: `feat/recruitment-demo`; baseline: `811bce7`.

## Local environment and repairs

macOS; Python 3.11 from Miniforge environment `resolveflow-demo`; Vue/Vite served
at `http://localhost:5173`. Browser acceptance used the local Codex in-app browser.
Existing `Claude outputs/` was left untouched. The remote branch was fetched and
had no additional commits before work began.

The first browser visit exposed a real 404: Vite resolved `localhost:8000` to a
Docker service on IPv6 while the isolated Demo listened on `127.0.0.1:8000`.
The proxy now targets IPv4 explicitly, and the UI rejects a non-Demo health response.
No duplicate frontend or conflicting backend was started; the known offline
backend was replaced on the same IPv4 port for live-model acceptance.

Other repairs:
- Wire Demo support agents to read-only error-code/context tools and a scoped local
  BM25 knowledge adapter. Tool descriptions and the UI explicitly label lexical
  retrieval; no claim of vector retrieval, reranking, or full hybrid RAG.
- Preserve support agent identity, tool traces, diagnostics and degradations in
  mixed results; present business and technical answers as separate chat messages.
- Resolve unique Pro/Basic aliases grounded in user text even when the model emits
  `Pro 会员`, without accepting an ungrounded model-only object guess.
- Disable role/reset/chat actions while decisions are running; clear stale errors
  and data on identity changes; deduplicate source chips.
- Compact the desktop introduction/chat layout, retain a readable composer, support
  320px widths, and hide unavailable production administration in Demo settings.

## Commands and outcomes

All Python commands below used
`/Users/zhenxiangzhu/miniforge3/envs/resolveflow-demo/bin/python`, **not** `.venv`.

| Command | Final result |
|---|---|
| `make -C ResolveFlow test PYTHON=<conda-python>` | 313 run: **304 passed, 9 skipped, 0 failures/errors** |
| `PYTHONPATH=. <conda-python> scripts/run_commerce_acceptance.py` | **42 passed**, no skips/failures |
| `PYTHONPATH=. <conda-python> -m unittest tests.test_portfolio_demo -v` | **8 passed**, no skips/failures; included in full suite |
| `npm run build --prefix ResolveFlowFrontend` | Passed |
| `git diff --check` | Passed |
| `scripts/check_portfolio_live.py --live --env-file .env.agent.local --output ../docs/live-validation.json` | **3/3 passed** against real Qwen Plus; evidence below |

The nine opt-in skips remain five Postgres POC tests (`AGENT_PG_TEST_DSN` absent)
and four Redis/Chroma dependency tests (`RF_DEPENDENCY_TEST` not enabled).
They were not removed or bypassed. ONNX telemetry / Chroma telemetry warnings were
nonfatal. No dependency installation was needed for the final local test run.

## Five scenarios — actual browser evidence

| Scenario | Observed result |
|---|---|
| 商品退款政策是什么？ | Policy text and three sources; opening a source reveals policy origin/document ID. No Case. Browser offline + real-model API interpretation verified. |
| 我要退款。 | Eight candidates, no selected object and no Case; verified in both offline and live browser modes. Explicit selection proceeds to quotation. |
| 帮我把无线耳机退掉。 | ¥259 quote and return/invoice impact → Confirm refund → Reviewer Approve → Simulate return received → Simulate payment receipt → User Completed. Refresh restores the Case. Double-clicked confirm/receipt; purchase details contain exactly one ¥259 simulated refund. |
| Basic 会员下个月别续了。 | User confirmation changes auto-renew to OFF; Basic benefits remain; no refund record. |
| 把 Pro 会员重复扣的钱退掉，而且登录一直报 401。 | Real Qwen interpretation returns the **duplicate P2 charge, ¥99**, awaiting confirmation. Separate **Technical Agent** answer; successful actual `lookup_error_code(code=401)` trace, no agent diagnostics. Both results shown in the UI. |

Reviewer identity and user confirmation remain distinct. API regressions verify
user approval rejection, pre-confirmation approval rejection, stale quotes,
owned records, idempotency and memory/authorization boundaries. No retired task
write route was re-enabled.

## Browser, isolation and responsive checks

- 1440×900, 1024×900, 390×844 and 320×800: document scroll width equals viewport
  width; desktop two-column / mobile stacked layouts visually inspected.
- Mobile refund confirmation and cancellation worked. Amounts, states and buttons
  remained readable. Chat remains the primary interaction; long answers scroll.
- Developer JSON and advanced controls are collapsed by default; new sessions
  provision without manual JWT entry. User/Reviewer labels and independent-review
  instructions were checked.
- Real-model loading indicator appears; scenario/role/reset/send controls disable
  during a request. Double-click transitions did not create duplicate refunds.
- A deliberately malformed test token clears old purchases/Cases/chat and shows a
  readable error. Reset recovers with eight fresh purchases and no old Case.
- Explicit identity A → B → A through Advanced settings: A's one Case disappears
  for B (eight fresh purchases, zero Cases), then returns for A. Cross-session
  direct object fetch returns 404. Old audits survive Reset.
- Browser console: **0 errors and 0 warnings in the final inspected session**.
  Initial wrong-service HTTP 404 was diagnosed and repaired; it is not hidden as
  a successful baseline. No cloud-browser limitation remained locally.

## Real model and retrieval evidence

Existing project `.env.agent.local` provided Qwen Plus / the DashScope OpenAI-
compatible endpoint. Only required configuration fields were inspected. No keys
were printed, replaced or committed. The real-model smoke test creates temporary
isolated sessions and writes only allowlisted evidence:
[three actual checks](live-validation.json).

The third check explicitly requested policy retrieval and error-code lookup;
Technical Agent actually invoked **both `knowledge_search` and
`lookup_error_code`** successfully. Source IDs came from the local policy corpus.
The `lexical_retrieval` marker is expected: this Demo has no dense-vector index or
reranker. This proves real tool execution, not merely `AGENT_USE_LLM=1`.

## Screenshots and scope

[Eight actual browser captures](screenshots/README.md), including the six requested
story images plus tablet/mobile evidence. Screenshots are unedited browser JPEGs;
mode labels remain visible where applicable. The mixed image is a **real model**
result, not an offline placeholder.

**Resume readiness:** accepted for a local AI product / Agent application portfolio
walkthrough. Claims should say controlled **simulated** after-sales execution,
real-model intent/support with read-only tools, and lexical policy retrieval.

Remaining limits: no actual payment/fulfilment/identity-provider integrations; no
production load or public-host security acceptance; no full hybrid-RAG acceptance;
model language output is nondeterministic and a successful smoke test is not a
reliability benchmark. Refresh restores Cases, not the rendered chat transcript.
Tokens expire after 24 hours. Old session databases need operator retention
management. These are declared product boundaries, not claimed successful tests.
