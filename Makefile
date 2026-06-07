# sml-beast-orchestrator — operator + dev shortcuts
#
# Run `make` (no args) to see the help text.

.PHONY: help install install-dev test test-fast lint format typecheck audit \
        ci clean preflight preflight-offline status balance dry-run \
        replies recent poll alerts-sweep kill-on kill-off

PY := python
PIP := $(PY) -m pip

# ── help ────────────────────────────────────────────────────────────────────

help:
	@echo "sml-beast-orchestrator — common commands"
	@echo ""
	@echo "  Dev:"
	@echo "    make install        Install package + bb7 extras"
	@echo "    make install-dev    Install package + bb7 + dev extras"
	@echo "    make test           Run full test suite"
	@echo "    make test-fast      Run tests excluding e2e + slow"
	@echo "    make lint           ruff check"
	@echo "    make format         ruff format"
	@echo "    make typecheck      mypy"
	@echo "    make audit          pip-audit (advisory)"
	@echo "    make ci             lint + typecheck + test (mirrors CI)"
	@echo "    make clean          Remove caches + build artifacts"
	@echo ""
	@echo "  BB7 operator:"
	@echo "    make preflight          Full preflight validator"
	@echo "    make preflight-offline  Preflight, skip network checks"
	@echo "    make status             bb7 status (full snapshot)"
	@echo "    make balance            bb7 balance (XRPL hot wallet)"
	@echo "    make dry-run            One cycle with NO XRPL/SMTP"
	@echo "    make replies            Operator review queue"
	@echo "    make recent             Last 20 state changes"
	@echo "    make poll               One-shot IMAP drain"
	@echo "    make alerts-sweep       Discord alert sweep"
	@echo "    make kill-on            Activate kill switch"
	@echo "    make kill-off           Deactivate kill switch"

# ── dev ──────────────────────────────────────────────────────────────────────

install:
	$(PIP) install -e ".[bb7]"

install-dev:
	$(PIP) install -e ".[bb7,dev]"
	$(PIP) install pytest

test:
	$(PY) -m pytest tests/ -q

test-fast:
	$(PY) -m pytest tests/ -q --ignore=tests/test_e2e_smoke.py

lint:
	$(PY) -m ruff check .

format:
	$(PY) -m ruff format .

typecheck:
	$(PY) -m mypy sml_beast --ignore-missing-imports

audit:
	$(PY) -m pip_audit

ci: lint typecheck test
	@echo "✓ ci gate green"

clean:
	rm -rf build/ dist/ *.egg-info/ .ruff_cache/ .mypy_cache/ .pytest_cache/
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

# ── BB7 operator shortcuts ──────────────────────────────────────────────────

preflight:
	$(PY) -m sml_beast.outreach.preflight

preflight-offline:
	$(PY) -m sml_beast.outreach.preflight --skip-network

status:
	$(PY) -m sml_beast.outreach.opctl status

balance:
	$(PY) -m sml_beast.outreach.opctl balance

dry-run:
	BB7_OUTREACH_DRY_RUN=1 $(PY) -m sml_beast.outreach.opctl dry-run

replies:
	$(PY) -m sml_beast.outreach.opctl replies

recent:
	$(PY) -m sml_beast.outreach.opctl recent 20

poll:
	$(PY) -m sml_beast.outreach.opctl poll

alerts-sweep:
	$(PY) -m sml_beast.outreach.opctl alerts-sweep

kill-on:
	$(PY) -m sml_beast.outreach.opctl kill on

kill-off:
	$(PY) -m sml_beast.outreach.opctl kill off
