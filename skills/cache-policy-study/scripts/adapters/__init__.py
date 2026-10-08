"""Adapter registry. Each module exposes:

    TOOL: str          registry id (matches the module's events' `tool`)
    VERIFIED: bool     True only if checked against a real store of this tool
    def collect(env: Env, since: float | None, until: float | None, cov: Coverage) -> Iterator[dict]

and must never raise on a missing or malformed store: record it on `cov` and yield nothing.
"""
import importlib

MODULES = ["claude_code", "codex", "vscode_copilot", "copilot_cli", "gemini_cli", "antigravity", "cursor"]


def load(name):
    return importlib.import_module(f"adapters.{name}")
