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
