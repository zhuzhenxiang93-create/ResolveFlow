PYTHON := $(if $(wildcard ../.venv/bin/python),$(abspath ../.venv/bin/python),python3)

.PHONY: agent-chat
.PHONY: approval-init approval-server agent-review
approval-init:
	PYTHONPATH=. $(PYTHON) scripts/approval_service.py --init-only
approval-server:
	PYTHONPATH=. $(PYTHON) scripts/approval_service.py
agent-review:
	PYTHONPATH=. $(PYTHON) scripts/approval_cli.py

agent-chat:
	PYTHONPATH=. $(PYTHON) scripts/agent_cli.py

.PHONY: mint-token
mint-token:
	PYTHONPATH=. $(PYTHON) scripts/mint_agent_token.py $(SUBJECT) $(ROLE)

.PHONY: pg-poc-up pg-poc-down pg-poc-test
pg-poc-up:
	docker compose up -d postgres

pg-poc-down:
	docker compose rm -sf postgres

pg-poc-test:
	AGENT_PG_TEST_DSN="postgresql://resolveflow:$${PG_POC_PASSWORD:-resolveflow123}@localhost:$${PG_POC_PORT:-15432}/resolveflow_action_poc" \
		PYTHONPATH=. $(PYTHON) -m unittest tests.test_action_runtime_pg_poc -v

.PHONY: test routing-eval supervisor-eval rag-eval rag-eval-rerank metrics-report eval-smoke eval-final agent-demo

agent-demo:
	PYTHONPATH=. $(PYTHON) scripts/run_agent_demo.py

.PHONY: agent-eval agent-eval-live
agent-eval:
	PYTHONPATH=. $(PYTHON) scripts/run_agent_eval.py

agent-eval-live:
	PYTHONPATH=. $(PYTHON) scripts/run_agent_eval.py --live

test:
	PYTHONPATH=. $(PYTHON) -m unittest discover -s tests

rag-eval:
	PYTHONPATH=. $(PYTHON) scripts/run_rag_eval.py

rag-eval-rerank:
	PYTHONPATH=. RAG_EVAL_USE_QWEN_RERANK=1 $(PYTHON) scripts/run_rag_eval.py

routing-eval:
	PYTHONPATH=. $(PYTHON) scripts/run_routing_eval.py --split holdout

supervisor-eval:
	PYTHONPATH=. $(PYTHON) scripts/run_supervisor_eval.py

metrics-report:
	PYTHONPATH=. $(PYTHON) scripts/metrics_report.py

eval-smoke: test routing-eval supervisor-eval metrics-report

eval-final: test routing-eval supervisor-eval rag-eval metrics-report

.PHONY: subscription-live-check unified-live-check summarize-p0

# Opt-in real-model smoke checks — not part of eval-smoke/eval-final since they spend
# real API budget; run explicitly when validating a live deployment.
subscription-live-check:
	PYTHONPATH=. $(PYTHON) scripts/check_subscription_live.py --live

unified-live-check:
	PYTHONPATH=. $(PYTHON) scripts/check_unified_live.py --live

summarize-p0:
	PYTHONPATH=. $(PYTHON) scripts/summarize_p0.py
