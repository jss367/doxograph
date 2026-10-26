"""Structured paper analysis through Codex's saved ChatGPT login."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

from . import config


def executable() -> str:
    explicit = os.environ.get("DOXOGRAPH_CODEX_PATH")
    if explicit:
        return str(Path(explicit).expanduser())
    found = shutil.which("codex")
    if found:
        return found
    candidates = [Path.home() / ".local/bin/codex", Path("/opt/homebrew/bin/codex"),
                  Path("/usr/local/bin/codex"),
                  Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
                  Path("/Applications/Codex.app/Contents/Resources/codex")]
    candidates.extend(sorted((Path.home() / ".nvm/versions/node").glob("*/bin/codex"),
                             key=lambda p: p.stat().st_mtime, reverse=True))
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    raise RuntimeError("Codex is not installed. Install the Codex CLI and run `codex login` in Terminal.")


def environment() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("OPENAI_", "ANTHROPIC_"))
           and k not in ("CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "CODEX_INTERNAL_ORIGINATOR_OVERRIDE")}
    binary = executable()
    env["PATH"] = str(Path(binary).parent) + os.pathsep + env.get("PATH", os.defpath)
    return env


def _run(args: list[str], *, env: dict, cwd: str, input: str | None = None,
         timeout: int = 600) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, input=input, capture_output=True, text=True,
                              env=env, cwd=cwd, timeout=timeout)
    except OSError as exc:
        raise RuntimeError("Codex could not start. Install the Codex CLI and run `codex login`.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Codex timed out. Try again or check your ChatGPT plan's usage limits.") from exc


class Client:
    def __init__(self):
        self.messages = self

    def create(self, **kwargs):
        config.require_ai_enabled()
        binary, env = executable(), environment()
        with tempfile.TemporaryDirectory(prefix="doxograph-codex-") as cwd:
            auth = _run([binary, "login", "status"], env=env, cwd=cwd, timeout=15)
            # Check before forcing an auth method: Codex may log out credentials
            # that conflict with forced_login_method. Never change the user's login.
            status = (auth.stdout + auth.stderr).strip()
            if auth.returncode or status != "Logged in using ChatGPT":
                raise RuntimeError("Sign in with ChatGPT using `codex login` in Terminal. "
                                   "Doxograph will not fall back to API billing.")
            prompt = []
            for message in kwargs["messages"]:
                blocks = message["content"]
                if isinstance(blocks, str):
                    blocks = [{"type": "text", "text": blocks}]
                for block in blocks:
                    if block.get("type") != "text":
                        raise RuntimeError("Codex requires extracted paper text. Choose Claude for PDF images.")
                    prompt.append(block["text"])
            stdin = "\n\n".join(prompt)
            if len(stdin) > 600_000:
                raise RuntimeError("This paper and conversation are too long for Codex. Use a shorter paper or Claude.")
            instructions = Path(cwd) / "instructions.txt"
            instructions.write_text(kwargs["system"], encoding="utf-8")
            output = Path(cwd) / "answer.json"
            args = [binary, "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
                    "--skip-git-repo-check", "--sandbox", "read-only", "--json", "--color", "never",
                    "-c", 'approval_policy="never"', "-c", 'forced_login_method="chatgpt"',
                    "-c", 'model_provider="openai"', "-c", 'web_search="disabled"',
                    "-c", "project_doc_max_bytes=0", "-c", "skills.include_instructions=false",
                    "-c", f"model_instructions_file={json.dumps(str(instructions))}",
                    "--output-last-message", str(output)]
            for feature in ("shell_tool", "shell_snapshot", "apps", "hooks", "plugins", "remote_plugin",
                            "multi_agent", "in_app_browser", "in_app_local_automation"):
                args.extend(["--disable", feature])
            if kwargs["model"] != "Codex default":
                args.extend(["--model", kwargs["model"]])
            effort = kwargs.get("output_config", {}).get("effort")
            if effort:
                args.extend(["-c", f"model_reasoning_effort={json.dumps(effort)}"])
            schema = kwargs.get("output_config", {}).get("format", {}).get("schema")
            if schema:
                schema_path = Path(cwd) / "schema.json"
                schema_path.write_text(json.dumps(schema), encoding="utf-8")
                args.extend(["--output-schema", str(schema_path)])
            args.append("-")
            config.require_ai_enabled()
            result = _run(args, env=env, cwd=cwd, input=stdin)
            try:
                events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
                if any(not isinstance(event, dict) for event in events):
                    raise ValueError("Expected JSON objects")
            except ValueError as exc:
                raise RuntimeError("Codex returned an unreadable response. Update Codex and retry.") from exc
            failed = next((event for event in reversed(events)
                           if event.get("type") in ("turn.failed", "error")), None)
            complete = next((event for event in reversed(events) if event.get("type") == "turn.completed"), None)
            if result.returncode or failed or not complete:
                detail = (failed or {}).get("message") or (failed or {}).get("error", {}).get("message")
                raise RuntimeError(f"Codex could not complete the request: {detail or result.stderr.strip() or 'no result'}")
            try:
                answer = output.read_text(encoding="utf-8").strip()
                if schema and not isinstance(json.loads(answer), dict):
                    raise ValueError("Expected an object")
                if not answer:
                    raise ValueError("Empty answer")
            except (OSError, ValueError) as exc:
                raise RuntimeError("Codex returned no complete answer. Retry the request.") from exc
        usage = complete.get("usage") or {}
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=answer)],
                               stop_reason="end_turn", usage=SimpleNamespace(**usage))
