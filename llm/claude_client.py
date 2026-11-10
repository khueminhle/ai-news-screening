"""Call Claude through the Claude Code CLI (`claude --print`), signed in with your subscription.

How one call works (same idea as the reference project, but Claude gets no tools):
  1. Python writes the prompt to a temp .txt file (too long for a command line).
  2. `claude --print ... < prompt.txt` : the file is fed in as the prompt.
  3. Claude's answer comes back through the pipe (stdout) into a Python string.
  4. The temp file is deleted. Python, not Claude, decides where anything is saved.

Sign-in: the logged-in Claude Code account on this machine, or CLAUDE_CODE_OAUTH_TOKEN
(from `claude setup-token`) in .env / the environment. ANTHROPIC_API_KEY is removed from
Claude's environment so the run can never be billed to the API by accident.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from connectors.base import PROJECT_ROOT

MODEL = "claude-haiku-4-5-20251001"   # same as the reference project: cheapest, fastest
EFFORT = "low"
TIMEOUT = 300                          # seconds one call may run before it is killed; None = no limit
MAX_PARALLEL = 10                      # max Claude processes at once
THINKING_TOKENS = 0                    # 0 = no extended thinking. Tested 2026-11-10 on a 10-item batch:
                                       # thinking on 151 s / 19k output tokens, off 15 s / 2k, same labels quality

# Claude runs from an empty folder, so no CLAUDE.md or project files are picked up.
EMPTY_CWD = PROJECT_ROOT / "data" / "llm" / "_claude_cwd"
SYSTEM_PROMPT = ("You are a careful financial news classifier. You have no tools. "
                 "Reply with one JSON object only, exactly as the user's instructions describe. No prose, no markdown.")
# Variables that would switch billing to the API or confuse a nested Claude Code.
DROP_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")


@dataclass
class ClaudeResult:
    ok: bool
    text: str                     # Claude's reply (the part we parse)
    returncode: int
    seconds: float
    error: str | None = None
    usage: dict = field(default_factory=dict)


def claude_command() -> list[str]:
    exe = os.environ.get("CLAUDE_CMD") or shutil.which("claude")   # CLAUDE_CMD lets tests use a fake claude
    if not exe:
        raise RuntimeError("Claude Code CLI not found. Install it: npm install -g @anthropic-ai/claude-code")
    return [exe]


def child_env() -> dict[str, str]:
    load_dotenv(PROJECT_ROOT / ".env")
    env = {k: v for k, v in os.environ.items() if k not in DROP_ENV}
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("CLAUDE_OAUTH_TOKEN")
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    if THINKING_TOKENS is not None:
        env["MAX_THINKING_TOKENS"] = str(THINKING_TOKENS)
    return env


def build_args(model: str, effort: str | None) -> list[str]:
    args = ["--print", "--model", model,
            "--tools", "",                     # no tools at all: Claude only sees the prompt
            "--system-prompt", SYSTEM_PROMPT,  # replaces Claude Code's long default prompt (fewer tokens)
            "--output-format", "json",         # reply + token usage in one JSON envelope
            "--strict-mcp-config",             # no MCP servers / connectors
            "--disable-slash-commands",
            "--no-session-persistence"]
    if effort:
        args += ["--effort", effort]
    return args


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    if sys.platform == "win32":
        # claude.cmd -> cmd.exe -> node: kill the whole tree or Claude keeps running
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        os.killpg(proc.pid, signal.SIGKILL)


def parse_envelope(stdout: str) -> tuple[str, dict, str | None]:
    """--output-format json gives {"result": "...", "is_error": false, "usage": {...}}. Plain text is accepted too."""
    try:
        env = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout.strip(), {}, None
    if not isinstance(env, dict) or "result" not in env:
        return stdout.strip(), {}, None
    usage = {k: env.get(k) for k in ("usage", "total_cost_usd", "duration_ms", "num_turns") if k in env}
    error = str(env.get("result"))[:500] if env.get("is_error") else None
    return str(env.get("result") or ""), usage, error


async def run_claude(prompt: str, *, model: str = MODEL, effort: str | None = EFFORT,
                     timeout: float | None = TIMEOUT) -> ClaudeResult:
    EMPTY_CWD.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as tf:
        tf.write(prompt)
        prompt_file = tf.name

    started = time.monotonic()
    try:
        with open(prompt_file, "rb") as stdin:          # same as `< prompt.txt`
            proc = await asyncio.create_subprocess_exec(
                *claude_command(), *build_args(model, effort),
                stdin=stdin, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=EMPTY_CWD, env=child_env(),
                start_new_session=sys.platform != "win32")
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                _kill_tree(proc)
                await proc.wait()
                return ClaudeResult(False, "", 124, time.monotonic() - started, f"TIMEOUT after {timeout}s -- Claude killed")
    finally:
        os.unlink(prompt_file)

    seconds = time.monotonic() - started
    stdout = out.decode("utf-8", errors="replace")
    stderr = err.decode("utf-8", errors="replace").strip()
    text, usage, envelope_error = parse_envelope(stdout)
    if proc.returncode != 0 or envelope_error:
        msg = envelope_error or stderr or text or f"exit code {proc.returncode}"
        return ClaudeResult(False, text, proc.returncode or 1, seconds, msg[:1000], usage)
    return ClaudeResult(True, text, 0, seconds, None, usage)


async def run_many(prompts: list[str], *, max_parallel: int = MAX_PARALLEL, **kwargs) -> list[ClaudeResult]:
    """Run several prompts, at most max_parallel at once. Results come back in the same order."""
    semaphore = asyncio.Semaphore(max_parallel)

    async def one(p: str) -> ClaudeResult:
        async with semaphore:      # the timeout only starts once this call has a slot
            return await run_claude(p, **kwargs)

    return await asyncio.gather(*(one(p) for p in prompts))


def main() -> None:
    """Quick sign-in check:  python -m llm.claude_client"""
    result = asyncio.run(run_claude('Reply with exactly this JSON: {"ok": true}'))
    print(f"ok={result.ok} seconds={result.seconds:.1f} error={result.error}")
    print("reply:", result.text[:300])
    if result.usage:
        print("usage:", json.dumps(result.usage)[:300])


if __name__ == "__main__":
    main()
