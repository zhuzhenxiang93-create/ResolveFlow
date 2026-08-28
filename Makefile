PYTHON := $(if $(wildcard .venv/bin/python),$(abspath .venv/bin/python),python3)

.PHONY: test eval-smoke eval-final routing-eval rag-eval metrics-report

test:
	$(MAKE) -C ResolveFlow test PYTHON=$(PYTHON)

rag-eval:
	$(MAKE) -C ResolveFlow rag-eval PYTHON=$(PYTHON)

routing-eval:
	$(MAKE) -C ResolveFlow routing-eval PYTHON=$(PYTHON)

metrics-report:
	$(MAKE) -C ResolveFlow metrics-report PYTHON=$(PYTHON)

eval-smoke:
	$(MAKE) -C ResolveFlow eval-smoke PYTHON=$(PYTHON)

eval-final:
	$(MAKE) -C ResolveFlow eval-final PYTHON=$(PYTHON)
