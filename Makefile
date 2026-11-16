.PHONY: test demo demo-safe report clean

test:
	PYTHONPATH=src python3 -m pytest -q

demo:
	PYTHONPATH=src python3 -m mcp_guardrail_probe.cli --demo

demo-safe:
	PYTHONPATH=src python3 -m mcp_guardrail_probe.cli --demo-safe

report:
	PYTHONPATH=src python3 -m mcp_guardrail_probe.cli --demo --out examples/sample_report.md

clean:
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
