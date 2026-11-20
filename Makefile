.PHONY: test demo demo-safe report clean

# The project venv when there is one, so `make test` works without activating
# it first; override with `make test PYTHON=python3.12`.
PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

test:
	PYTHONPATH=src $(PYTHON) -m pytest -q

demo:
	PYTHONPATH=src $(PYTHON) -m mcp_guardrail_probe.cli --demo

demo-safe:
	PYTHONPATH=src $(PYTHON) -m mcp_guardrail_probe.cli --demo-safe

# --demo exits 1 because the vulnerable mock has sev1 findings; that is the
# expected result here, not a failed build.
report:
	PYTHONPATH=src $(PYTHON) -m mcp_guardrail_probe.cli --demo --out examples/sample_report.md || test $$? -eq 1

clean:
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
