#!/usr/bin/env bash
set -euo pipefail
repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
if [[ ! -f "$repo_dir/.env.dify" ]]; then
  echo "Missing .env.dify: configure the Dify application API credential first." >&2
  exit 1
fi
set -a
source "$repo_dir/.env.dify"
set +a
export RESOLVEFLOW_DEMO_MODE=true
export RESOLVEFLOW_DEMO_DIR="${RESOLVEFLOW_DEMO_DIR:-/tmp/resolveflow-dify-demo}"
cd "$repo_dir/ResolveFlow"
exec "${RESOLVEFLOW_PYTHON:-python3.11}" -m uvicorn api.portfolio_demo:create_app --factory --host 127.0.0.1 --port "${RESOLVEFLOW_PORT:-8000}"
