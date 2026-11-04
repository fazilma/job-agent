"""Thin wrapper over the Claude Code CLI in headless (-p) mode.

Uses your existing Claude login, so no separate API key is needed.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path


class ClaudeError(RuntimeError):
    pass


class UsageLimitError(ClaudeError):
    """Your Claude plan's session/weekly limit is reached. Retrying now only fails again."""


def _is_usage_limit(text: str) -> bool:
    t = (text or "").lower()
    return "usage_limit_reached" in t or "hit your session limit" in t or "hit your weekly limit" in t


# Usage of the most recent call (cost estimate, tokens, turns). Read by callers that log it.
last_usage: dict = {}


def _usage_from(payload: dict, secs: float, model: str | None) -> dict:
    u = payload.get("usage") or {}
    models = list((payload.get("modelUsage") or {}).keys())
    return {
        "model": models[0] if models else (model or ""),
        "cost_usd": float(payload.get("total_cost_usd") or 0),
        "input_tokens": int(u.get("input_tokens") or 0),
        "output_tokens": int(u.get("output_tokens") or 0),
        "cache_read_tokens": int(u.get("cache_read_input_tokens") or 0),
        "cache_write_tokens": int(u.get("cache_creation_input_tokens") or 0),
        "turns": int(payload.get("num_turns") or 0),
        "secs": round(secs, 1),
    }


def claude_bin() -> str:
    path = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if not path:
        for guess in ("~/.claude/local/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude"):
            p = Path(os.path.expanduser(guess))
            if p.exists():
                return str(p)
        raise ClaudeError("Claude Code CLI not found. Install it or set CLAUDE_BIN in .env")
    return path


def run(prompt: str, *, model: str | None = None, cwd: Path | None = None, timeout: int = 300,
        extra_args: list[str] | None = None) -> str:
    """Run one headless prompt and return the final text."""
    cmd = [claude_bin(), "-p", "--output-format", "json"]
    if model:
        cmd += ["--model", model]
    cmd += extra_args or []
    import time as _time
    last_usage.clear()
    t0 = _time.monotonic()           # stops while the Mac sleeps, like the timeout below
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                              cwd=str(cwd) if cwd else None, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        last_usage.update({"model": model or "", "secs": round(_time.monotonic() - t0, 1), "ok": False})
        raise ClaudeError(f"timed out after {timeout}s") from e
    try:
        _payload = json.loads(proc.stdout)
        if isinstance(_payload, dict):
            last_usage.update(_usage_from(_payload, _time.monotonic() - t0, model))
            last_usage["ok"] = proc.returncode == 0 and not _payload.get("is_error")
    except (json.JSONDecodeError, TypeError):
        last_usage.update({"model": model or "", "secs": round(_time.monotonic() - t0, 1), "ok": proc.returncode == 0})
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "unknown error").strip()
        readable = _limit_message(proc.stdout)          # the CLI's own one-line message, if any
        if _is_usage_limit(err):
            raise UsageLimitError(readable or "Claude usage limit reached")
        raise ClaudeError((readable or err)[-2000:])
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return proc.stdout
    if payload.get("is_error"):
        if payload.get("api_error") == "usage_limit_reached" or _is_usage_limit(str(payload.get("result"))):
            raise UsageLimitError(str(payload.get("result") or "Claude usage limit reached"))
        raise ClaudeError(str(payload.get("result"))[-2000:])
    return payload.get("result", "")


def _limit_message(stdout: str) -> str:
    try:
        return str(json.loads(stdout).get("result") or "")
    except Exception:
        return ""


def extract_json(text: str):
    """Pull the first JSON array/object out of a model reply (tolerates ```json fences)."""
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1)
    for opener, closer in (("[", "]"), ("{", "}")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("no JSON found in model reply")
