"""Structured paper analysis through Codex's saved ChatGPT login."""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Unpack

from PIL import Image

from . import config
from .analysis_types import ModelResponse, ProviderRequest
from .model_errors import InputTooLargeError, ModelInputError, ModelSetupError


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
    raise ModelSetupError("Codex is not installed. Install the Codex CLI and run `codex login` in Terminal.")


def environment() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("OPENAI_", "ANTHROPIC_"))
           and k not in ("CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "CODEX_INTERNAL_ORIGINATOR_OVERRIDE")}
    binary = executable()
    env["PATH"] = str(Path(binary).parent) + os.pathsep + env.get("PATH", os.defpath)
    return env


def _run(args: list[str], *, env: dict[str, str], cwd: str, input: str | None = None,
         timeout: int = 600) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, input=input, capture_output=True, text=True,
                              env=env, cwd=cwd, timeout=timeout)
    except OSError as exc:
        raise ModelSetupError("Codex could not start. Install the Codex CLI and run `codex login`.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Codex timed out. Try again or check your ChatGPT plan's usage limits.") from exc


def models() -> list[dict[str, str]]:
    """Read Codex's model catalog without starting an analysis turn."""
    binary, env = executable(), environment()
    message = "Could not load models from Codex. Update the Codex CLI or enter a custom model ID."
    try:
        with tempfile.TemporaryDirectory(prefix="doxograph-codex-models-") as cwd:
            result = _run([binary, "-c", 'model_provider="openai"', "debug", "models"],
                          env=env, cwd=cwd, timeout=15)
    except RuntimeError as exc:
        raise ModelSetupError(message) from exc
    if result.returncode:
        raise ModelSetupError(message)
    try:
        catalog = json.loads(result.stdout)["models"]
        if not isinstance(catalog, list) or not all(isinstance(item, dict) for item in catalog):
            raise ValueError("Expected a model catalog")
        # Codex orders its recommendations by priority. Hidden models are not
        # offered, but a saved or custom model can still be selected explicitly.
        visible = [item for item in catalog if item.get("visibility") == "list"
                   and isinstance(item.get("slug"), str) and item["slug"]]
        visible.sort(key=lambda item: item.get("priority") if isinstance(item.get("priority"), int) else 999)
        return [{"id": item["slug"], "name": item["display_name"]
                 if isinstance(item.get("display_name"), str) and item["display_name"] else item["slug"]}
                for item in visible]
    except (ValueError, KeyError, TypeError) as exc:
        raise ModelSetupError(message) from exc


@dataclass(frozen=True)
class MCPServer:
    name: str
    command: str
    args: list[str]
    env: dict[str, str]
    enabled_tools: tuple[str, ...]

    def config_args(self) -> list[str]:
        """CLI overrides use TOML values; encode each leaf separately."""
        prefix = f"mcp_servers.{self.name}"
        values = {f"{prefix}.command": json.dumps(self.command),
                  f"{prefix}.args": json.dumps(self.args),
                  f"{prefix}.enabled_tools": json.dumps(self.enabled_tools),
                  f"{prefix}.required": "true"}
        values.update({f"{prefix}.env.{key}": json.dumps(value) for key, value in self.env.items()})
        return [arg for key, value in values.items() for arg in ("-c", f"{key}={value}")]


class Client:
    def __init__(self, *, mcp_server: Callable[[], MCPServer] | None = None) -> None:
        self.messages = self
        # Resolve the callback only after checking login, before starting a turn.
        self.mcp_server = mcp_server
        self.events: list[dict[str, Any]] = []

    def create(self, **kwargs: Unpack[ProviderRequest]) -> ModelResponse:
        config.require_ai_enabled()
        binary, env = executable(), environment()
        with tempfile.TemporaryDirectory(prefix="doxograph-codex-") as cwd:
            auth = _run([binary, "login", "status"], env=env, cwd=cwd, timeout=15)
            # Check before forcing an auth method: Codex may log out credentials
            # that conflict with forced_login_method. Never change the user's login.
            status = (auth.stdout + auth.stderr).strip()
            if auth.returncode or status != "Logged in using ChatGPT":
                raise ModelSetupError("Sign in with ChatGPT using `codex login` in Terminal. "
                                   "Doxograph will not fall back to API billing.")
            prompt = []
            images: list[Path] = []
            for message in kwargs["messages"]:
                blocks = message["content"]
                if isinstance(blocks, str):
                    blocks = [{"type": "text", "text": blocks}]
                for block in blocks:
                    if block.get("type") == "image":
                        # Normalize GIF/WebP too, using generated paths rather than user filenames.
                        path = Path(cwd) / f"image-{len(images) + 1}.png"
                        with Image.open(io.BytesIO(base64.b64decode(block["source"]["data"]))) as image:
                            image.convert("RGB").save(path)
                        images.append(path)
                        prompt.append(f"[Attached image {len(images)}]")
                        continue
                    if block.get("type") != "text":
                        raise ModelInputError("Codex requires extracted paper text. Choose Claude for PDF images.")
                    prompt.append(block["text"])
            stdin = "\n\n".join(prompt)
            if len(stdin) > 600_000:
                raise InputTooLargeError("This paper and conversation are too long for Codex. Use a shorter paper or Claude.")
            instructions = Path(cwd) / "instructions.txt"
            instructions.write_text(kwargs["system"], encoding="utf-8")
            output = Path(cwd) / "answer.json"
            # These flags exclude config.toml and project guidance, but Codex
            # still includes global AGENTS instructions from its home. The
            # model-access controls and chat dialog disclose that behavior.
            args = [binary, "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
                    "--skip-git-repo-check", "--sandbox", "read-only", "--json", "--color", "never",
                    "-c", 'approval_policy="never"', "-c", 'forced_login_method="chatgpt"',
                    "-c", 'model_provider="openai"', "-c", 'web_search="disabled"',
                    "-c", "tools.view_image=false",
                    "-c", "project_doc_max_bytes=0", "-c", "skills.include_instructions=false",
                    "-c", f"model_instructions_file={json.dumps(str(instructions))}",
                    "--output-last-message", str(output)]
            for feature in ("shell_tool", "shell_snapshot", "apps", "hooks", "plugins", "remote_plugin",
                            "multi_agent", "in_app_browser", "in_app_local_automation", "browser_use",
                            "computer_use", "image_generation", "skill_search", "tool_suggest"):
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
            if self.mcp_server is not None:
                args.extend(self.mcp_server().config_args())
            # Keep the positional prompt out of the variadic image option.
            args.append("-")
            for image_path in images:
                args.extend(["--image", str(image_path)])
            config.require_ai_enabled()
            result = _run(args, env=env, cwd=cwd, input=stdin)
            try:
                events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
                if any(not isinstance(event, dict) for event in events):
                    raise ValueError("Expected JSON objects")
            except ValueError as exc:
                raise RuntimeError("Codex returned an unreadable response. Update Codex and retry.") from exc
            self.events = events
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
        return ModelResponse.from_text(answer, usage)
