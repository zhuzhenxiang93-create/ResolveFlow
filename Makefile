PYTHON := $(if $(wildcard .venv/bin/python),$(abspath .venv/bin/python),python3)

.PHONY: agent-chat
.PHONY: approval-init approval-server agent-review
approval-init approval-server agent-review:
	$(MAKE) -C ResolveFlow $@ PYTHON=$(PYTHON)

agent-chat:
	$(MAKE) -C ResolveFlow agent-chat PYTHON=$(PYTHON)

.PHONY: mint-token
mint-token:
	$(MAKE) -C ResolveFlow mint-token PYTHON=$(PYTHON) SUBJECT=$(SUBJECT) ROLE=$(ROLE)

.PHONY: pg-poc-up pg-poc-down pg-poc-test
pg-poc-up pg-poc-down pg-poc-test:
	$(MAKE) -C ResolveFlow $@ PYTHON=$(PYTHON)

.PHONY: test eval-smoke eval-final routing-eval rag-eval metrics-report agent-demo

agent-demo:
	$(MAKE) -C ResolveFlow agent-demo PYTHON=$(PYTHON)

.PHONY: agent-eval agent-eval-live
agent-eval:
	$(MAKE) -C ResolveFlow agent-eval PYTHON=$(PYTHON)

agent-eval-live:
	$(MAKE) -C ResolveFlow agent-eval-live PYTHON=$(PYTHON)

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
