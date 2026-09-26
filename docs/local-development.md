# Local development

Python 3.11+ and Node 20.19+ / 22.12+ are required. From the repository root:

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r ResolveFlow/requirements.txt
npm ci --prefix ResolveFlowFrontend
```

Terminal 1:

```bash
cd ResolveFlow
RESOLVEFLOW_DEMO_MODE=true AGENT_USE_LLM=0 ../.venv/bin/uvicorn api.portfolio_demo:create_app --factory --host 127.0.0.1 --port 8000
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
The [full backend](../ResolveFlow/README.md) provides Redis/ChromaDB and hybrid RAG.

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

[Sanitized real-provider evidence](live-validation.json).


## Verification

From the repository root, with the appropriate Python interpreter:

```bash
make test PYTHON="$(pwd)/.venv/bin/python"
npm run build --prefix ResolveFlowFrontend
cd ResolveFlow
PYTHONPATH=. ../.venv/bin/python scripts/run_commerce_acceptance.py
```

[Acceptance results](validation.md) · [Back to project](../README.md)
