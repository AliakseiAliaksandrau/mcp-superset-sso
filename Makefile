VERSION := $(shell grep '^version' pyproject.toml | head -1 | sed 's/.*"\(.*\)"/\1/')

.PHONY: help run run-bg stop lint format build clean install dev check release release-test

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

# ── Development ──────────────────────────────────────────────

install:  ## Install package in editable mode
	uv pip install -e .

dev:  ## Install with dev dependencies
	uv pip install -e ".[dev]"

run:  ## Run MCP server (kills existing on port 8001 first)
	@lsof -ti tcp:8001 2>/dev/null | xargs kill -9 2>/dev/null; sleep 0.3
	uv run python -m mcp_superset

run-bg:  ## Run MCP server in background
	@lsof -ti tcp:8001 2>/dev/null | xargs kill -9 2>/dev/null; sleep 0.3
	@nohup uv run python -m mcp_superset > /tmp/mcp_superset.log 2>&1 & echo "PID: $$!"
	@echo "Logs: tail -f /tmp/mcp_superset.log"

stop:  ## Stop running MCP server
	@lsof -ti tcp:8001 2>/dev/null | xargs kill -9 2>/dev/null && echo "Stopped" || echo "Not running"

logs:  ## Show MCP server logs
	@tail -f /tmp/mcp_superset.log

# ── Code Quality ─────────────────────────────────────────────

lint:  ## Run linter (ruff check)
	uv run ruff check src/

format:  ## Format code (ruff format)
	uv run ruff format src/

check:  ## Run all checks (lint + format check + build)
	uv run ruff check src/
	uv run ruff format --check src/
	uv build
	uv run twine check dist/*
	@echo "\n\033[32mAll checks passed!\033[0m"

# ── Build & Publish ──────────────────────────────────────────

build:  ## Build package (sdist + wheel)
	uv build

clean:  ## Clean build artifacts
	rm -rf dist/ build/ src/*.egg-info .ruff_cache .mypy_cache

release:  ## Create GitHub release → auto-publish to PyPI (usage: make release)
	@echo "Current version: $(VERSION)"
	@echo ""
	@read -p "Create release v$(VERSION)? [y/N] " confirm && [ "$$confirm" = "y" ] || exit 1
	git tag -a "v$(VERSION)" -m "Release v$(VERSION)"
	git push origin "v$(VERSION)"
	gh release create "v$(VERSION)" --title "v$(VERSION)" --generate-notes
	@echo "\n\033[32mRelease v$(VERSION) created! PyPI publish will start automatically.\033[0m"
	@echo "Track: gh run list --workflow=publish.yml"

release-test:  ## Build and upload to TestPyPI (manual)
	uv build
	uv run twine upload --repository testpypi dist/*
	@echo "\nCheck: https://test.pypi.org/project/mcp-superset/"

# ── Info ─────────────────────────────────────────────────────

version:  ## Show current version
	@echo "$(VERSION)"

status:  ## Show server status
	@lsof -ti tcp:8001 >/dev/null 2>&1 && echo "Running (PID: $$(lsof -ti tcp:8001))" || echo "Not running"
