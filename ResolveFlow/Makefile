PYTHON := $(if $(wildcard ../.venv/bin/python),$(abspath ../.venv/bin/python),python3)

.PHONY: test routing-eval supervisor-eval rag-eval rag-eval-rerank metrics-report eval-smoke eval-final

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
