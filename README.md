# ResolveFlow

**AI Customer Service with Controlled Business Actions**

An AI customer service application that answers policy questions, queries owned
purchases, and carries after-sales requests through explicit confirmation,
independent review and verifiable simulated results.

> Local recruiting-demo acceptance passed: browser refund lifecycle, identity
> isolation, desktop/mobile layouts and real Qwen Plus + read-only tools verified.
> Payments are simulated; retrieval is lexical. See [validation](docs/validation.md).

## Demo / screenshots

[30-second / 3-minute / 5-minute walkthrough](docs/demo-script.md) · [Actual screenshots](docs/screenshots/README.md)

![ResolveFlow home](docs/screenshots/01-home.jpg)

![Real Technical Agent and duplicate-charge quote](docs/screenshots/06-mixed.jpg)

| Answer | Query | Action |
|---|---|---|
| Refund policies with source references | Purchases, invoices, shipments, subscriptions and refunds | Refunds, partial refunds, cancel renewal, cancel unpaid orders and repair benefits |
| Policy questions create no refund case | Only the authenticated user's records | Backend amounts, explicit consent and independent review |

## Architecture

![ResolveFlow architecture](docs/architecture.svg)

The current execution path is `/chat → ConversationService → CommerceConversation
→ CommerceStore`. General enquiries use `IntentRecognizer → AgentOrchestrator`.
Historical ActionRuntime code is retained for compatibility and historical tests;
it is not the frontend's execution engine.

## Try these scenarios

1. **Policy:** 商品退款政策是什么？
2. **Disambiguation:** 我要退款。
3. **Full refund:** 帮我把无线耳机退掉。
4. **Keep benefits, stop renewal:** Basic 会员下个月别续了。
5. **Mixed request:** 把 Pro 会员重复扣的钱退掉，而且登录一直报 401。

## Quick start

Python 3.11+ and Node 20.19+ / 22.12+ are required. From the repository root:

```bash
python3 -m venv .venv
.venv/bin/pip install -r ResolveFlow/requirements.txt
npm ci --prefix ResolveFlowFrontend
```

Terminal 1:

```bash
cd ResolveFlow
RESOLVEFLOW_DEMO_MODE=true ../.venv/bin/uvicorn api.portfolio_demo:create_app --factory --host 127.0.0.1 --port 8000
```

Terminal 2:

```bash
cd ResolveFlowFrontend
VITE_DEMO_MODE=true npm run dev
```

Open `http://localhost:5173`. Demo user and reviewer identities and eight purchase
records are provisioned automatically. Use **Reset Demo** for a fresh session.
The signing key and per-session databases live under `/tmp/resolveflow-portfolio`;
set `RESOLVEFLOW_DEMO_DIR` to a private persistent directory if restart persistence
is needed. Tokens expire after 24 hours; reset starts another session.

The default demo uses deterministic rules, lexical policy retrieval, and SQLite
memory. It does not claim live LLM or hybrid RAG execution. The interface labels
this mode. For real general-support agents, set `AGENT_USE_LLM=1`, `LLM_API_KEY`,
`LLM_MODEL`, and optionally `LLM_BASE_URL` / `LLM_PROVIDER` in the server environment.
No credentials are embedded in the frontend. Real Qwen Plus acceptance passed,
including actual error-code and policy-search tool calls. The isolated host still
uses lexical retrieval; this is not full hybrid-RAG acceptance.
The [full backend](ResolveFlow/README.md) provides Redis/ChromaDB and hybrid RAG.

### Existing Mac / Miniforge environment

Use the existing Python 3.11 environment rather than macOS Python 3.9 or a stale
`.venv`. From the repository root:

```bash
cd ResolveFlow
RESOLVEFLOW_DEMO_MODE=true AGENT_USE_LLM=0 \
  "$HOME/miniforge3/bin/conda" run --no-capture-output -n resolveflow-demo \
  python -m uvicorn api.portfolio_demo:create_app --factory --host 127.0.0.1 --port 8000
```

For live mode, reuse an **existing project** environment file without editing it
(the file stays ignored). Stop the old backend before changing modes:

```bash
RESOLVEFLOW_DEMO_MODE=true AGENT_USE_LLM=1 \
  "$HOME/miniforge3/bin/conda" run --no-capture-output -n resolveflow-demo \
  python -m dotenv -f .env.agent.local run -- \
  python -m uvicorn api.portfolio_demo:create_app --factory --host 127.0.0.1 --port 8000
```

Vite explicitly proxies to `127.0.0.1:8000`: on this Mac, `localhost:8000` can
resolve to a different Docker service listening on IPv6. The UI rejects a
non-Demo health response instead of showing a misleading connected state.

Run the local regression suite with an explicit interpreter:

```bash
make -C ResolveFlow test PYTHON="$HOME/miniforge3/envs/resolveflow-demo/bin/python"
# From ResolveFlow/; --live authorizes real provider calls:
PYTHONPATH=. "$HOME/miniforge3/envs/resolveflow-demo/bin/python" \
  scripts/check_portfolio_live.py --live --env-file .env.agent.local
```

[Sanitized real-provider evidence](docs/live-validation.json).

## Technology

Vue 3 · Vite · FastAPI · Pydantic · SQLite · provider-compatible LLM clients.
The full stack also includes Redis, ChromaDB, BM25/dense retrieval and RRF.

## Reliability design

- Object ownership is checked in the backend; ambiguous refunds require selection.
- Quotes bind an object, amount and version. Changed or expired evidence needs a new confirmation.
- Refund approval requires a different identity from the customer.
- Persisted decisions make repeated execution idempotent.
- Memory preferences cannot change eligibility or authorization.
- All payment and fulfilment effects are explicitly simulated.
- The demo host is separate from the production application, with a ledger per session.

## Validation

```bash
make test
npm run build --prefix ResolveFlowFrontend
cd ResolveFlow
PYTHONPATH=. ../.venv/bin/python scripts/run_commerce_acceptance.py
```

[Current results and outstanding acceptance](docs/validation.md).
Tests are developer-authored regressions; they are not a public benchmark.

## Limitations

No real payment gateway, fulfilment provider or production identity provider.
The isolated host is intended for local interviews; public hosting needs lifecycle,
rate and resource controls. Reset retains old audit databases; operators manage
retention. Offline rules have a limited language scope. Technical Agent answers
require a model connection. Browser layouts were checked at 1440, 1024, 390 and
320px. Model responses are nondeterministic; smoke checks are not reliability
benchmarks. Refresh restores Cases, not the rendered chat transcript.

[Backend guide](ResolveFlow/README.md) · [Frontend guide](ResolveFlowFrontend/README.md)
