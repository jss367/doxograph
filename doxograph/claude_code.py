"""Use the installed Claude Code CLI without handling subscription credentials."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

from . import config
from .model_errors import InputTooLargeError, ModelSetupError


def executable() -> str:
    explicit = os.environ.get("DOXOGRAPH_CLAUDE_PATH")
    if explicit:
        return str(Path(explicit).expanduser())
    found = shutil.which("claude")
    if found:
        return found
    # Dock launches don't inherit the shell's PATH, including nvm's bin dir.
    candidates = [Path.home() / ".local/bin/claude", Path("/opt/homebrew/bin/claude"),
                  Path("/usr/local/bin/claude")]
    candidates.extend(sorted((Path.home() / ".nvm/versions/node").glob("*/bin/claude"),
                             key=lambda p: p.stat().st_mtime, reverse=True))
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    raise ModelSetupError("Claude Code is not installed. Install it and run `claude auth login` in Terminal.")


def environment() -> dict[str, str]:
    # An inherited API key takes precedence over the subscription. Never
    # silently spend API credits when the user chose the subscription route.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("ANTHROPIC_", "CLAUDE_CODE_USE_"))
           and k not in ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDECODE")}
    binary = executable()
    env["PATH"] = str(Path(binary).parent) + os.pathsep + env.get("PATH", os.defpath)
    return env


def _run(args: list[str], *, env: dict, cwd: str, input: str | None = None,
         timeout: int = 600) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, input=input, capture_output=True, text=True,
                              env=env, cwd=cwd, timeout=timeout)
    except OSError as exc:
        raise ModelSetupError("Claude Code could not start. Install it and run `claude auth login`.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Claude Code timed out. Try again or check your plan's usage limits.") from exc


class Client:
    def __init__(self):
        self.messages = self

    def create(self, **kwargs):
        config.require_ai_enabled()
        binary, env = executable(), environment()
        # No project instructions, hooks, plugins, tools, or MCP connections.
        # --bare cannot be used: it deliberately disables subscription login.
        with tempfile.TemporaryDirectory(prefix="doxograph-claude-") as cwd:
            base = [binary, "--setting-sources", ""]
            auth = _run([*base, "auth", "status"], env=env, cwd=cwd, timeout=15)
            try:
                status = json.loads(auth.stdout)
            except ValueError:
                status = {}
            if not isinstance(status, dict):
                status = {}
            if auth.returncode or not status.get("loggedIn") or status.get("authMethod") != "claude.ai":
                raise ModelSetupError("Sign in to a Claude subscription with `claude auth login` in Terminal. "
                                   "Doxograph will not fall back to API billing.")
            content = []
            for message in kwargs["messages"]:
                blocks = message["content"]
                if isinstance(blocks, str):
                    blocks = [{"type": "text", "text": blocks}]
                content.extend(blocks)
            event = {"type": "user", "message": {"role": "user", "content": content}}
            stdin = json.dumps(event) + "\n"
            if len(stdin.encode("utf-8")) > 9_500_000:
                raise InputTooLargeError("This paper and conversation exceed Claude Code's input limit. "
                                   "Use a smaller PDF or choose Anthropic API in Settings.")
            args = [*base, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
                    "--verbose", "--model", kwargs["model"], "--system-prompt", kwargs["system"],
                    "--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                    "--settings", '{"disableAllHooks":true}', "--disable-slash-commands", "--no-chrome",
                    "--no-session-persistence", "--permission-mode", "dontAsk"]
            schema = kwargs.get("output_config", {}).get("format", {}).get("schema")
            if schema:
                args.extend(["--json-schema", json.dumps(schema)])
            effort = kwargs.get("output_config", {}).get("effort")
            if effort:
                args.extend(["--effort", effort])
            config.require_ai_enabled()
            result = _run(args, env=env, cwd=cwd, input=stdin)
        events = []
        try:
            events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
            if any(not isinstance(event, dict) for event in events):
                raise ValueError("Expected JSON objects")
        except ValueError as exc:
            raise RuntimeError("Claude Code returned an unreadable response. Update Claude Code and retry.") from exc
        final = next((e for e in reversed(events) if e.get("type") == "result"), {})
        if result.returncode or final.get("is_error") or not final:
            detail = final.get("result") or "; ".join(final.get("errors") or [])
            raise RuntimeError(f"Claude Code could not complete the request: {detail or result.stderr.strip() or 'no result'}")
        answer = final.get("structured_output") if schema else final.get("result")
        if (schema and not isinstance(answer, dict)) or (not schema and not isinstance(answer, str)):
            raise RuntimeError("Claude Code returned no complete answer. Retry the request.")
        usage = final.get("usage") or {}
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(answer) if schema else answer)],
                               stop_reason="end_turn", usage=SimpleNamespace(**usage))
