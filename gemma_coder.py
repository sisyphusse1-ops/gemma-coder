#!/usr/bin/env python3
"""gemma-coder — a local/edge agentic coding assistant powered by Gemma 4.

Why: Demonstrates that Gemma 4 E2B (2B effective params) can drive a useful
coding agent with the right rulebook and tool scoping. Ships the 12-rule
CLAUDE.md baseline from claude-code-pro-pack. Works against a local Ollama
endpoint OR OpenRouter's free Gemma 4 tier.

Usage:
    gemma-coder "fix the failing test in tests/test_api.py"
    gemma-coder --ollama "..." --model gemma4-e2b
    gemma-coder --audit CLAUDE.md   # run the cc-audit linter on project
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

DEFAULT_PROVIDER = os.environ.get("GEMMA_CODER_PROVIDER", "openrouter")  # openrouter | ollama
DEFAULT_MODEL = os.environ.get("GEMMA_CODER_MODEL", "google/gemma-4-26b-a4b-it:free")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/chat")
MAX_ITER = 12
MAX_INPUT_CHARS = 120_000  # fits well inside 256k Gemma ctx with headroom


# ---------------------------------------------------------------------------
# TOOL DEFINITIONS (JSON-native so we don't depend on Gemma's function-calling)
# ---------------------------------------------------------------------------

TOOL_SCHEMA = """
The only way you can act on the filesystem is by emitting EXACTLY ONE tool call
per reply in the form:

    <tool>
    {"name": "<tool_name>", "args": {...}}
    </tool>

Available tools:

  read_file(path)
      -> {"content": "...", "total_lines": N}
  write_file(path, content)
      -> {"bytes": N}
  search(pattern, path=".", glob="*")
      -> {"matches": [{"file": "...", "line": N, "text": "..."}]}
  run(cmd, cwd=".", timeout=60)
      -> {"stdout": "...", "stderr": "...", "exit": N}
  patch(path, old, new)
      -> {"bytes": N}
  done(summary)
      -> ends the loop. "summary" is shown to the user.

Reply with your reasoning in plain text, then the single <tool>...</tool>
block as the LAST thing in your message. Never emit code blocks around the
tool block. Never emit more than one tool call per reply.
""".strip()


SYSTEM_PROMPT_TEMPLATE = """
You are a careful coding agent running on Gemma 4. Follow the rulebook
provided below EXACTLY. Produce a single tool call per reply.

{tool_schema}

=== PROJECT RULEBOOK (CLAUDE.md / AGENTS.md) ===
{rulebook}
=== END RULEBOOK ===

Working directory: {cwd}
User request: {task}
""".strip()


# ---------------------------------------------------------------------------
# TOOLS
# ---------------------------------------------------------------------------

@dataclass
class ToolResult:
    ok: bool
    value: Any

    def to_dict(self) -> dict:
        return {"ok": self.ok, "value": self.value}


def tool_read_file(path: str, **_) -> ToolResult:
    p = Path(path)
    if not p.exists():
        return ToolResult(False, f"path not found: {path}")
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return ToolResult(False, str(e))
    return ToolResult(True, {"content": text[:MAX_INPUT_CHARS],
                             "total_lines": text.count("\n") + 1,
                             "truncated": len(text) > MAX_INPUT_CHARS})


def tool_write_file(path: str, content: str, **_) -> ToolResult:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text(content, encoding="utf-8")
    except Exception as e:
        return ToolResult(False, str(e))
    return ToolResult(True, {"bytes": len(content)})


def tool_search(pattern: str, path: str = ".", glob: str = "*", **_) -> ToolResult:
    cmd = ["rg", "--json", "-n", pattern, path]
    if glob and glob != "*":
        cmd[-2:-1] = ["-g", glob]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        # fallback to grep
        out = subprocess.run(["grep", "-rn", pattern, path],
                             capture_output=True, text=True, timeout=30)
        matches = []
        for line in out.stdout.splitlines()[:50]:
            parts = line.split(":", 2)
            if len(parts) == 3:
                matches.append({"file": parts[0], "line": int(parts[1]) if parts[1].isdigit() else 0,
                                "text": parts[2][:200]})
        return ToolResult(True, {"matches": matches})
    matches = []
    for line in out.stdout.splitlines():
        try:
            ev = json.loads(line)
            if ev.get("type") == "match":
                d = ev["data"]
                matches.append({
                    "file": d["path"]["text"],
                    "line": d["line_number"],
                    "text": d["lines"]["text"].rstrip()[:200],
                })
                if len(matches) >= 50:
                    break
        except json.JSONDecodeError:
            continue
    return ToolResult(True, {"matches": matches})


def tool_run(cmd: str, cwd: str = ".", timeout: int = 60, **_) -> ToolResult:
    try:
        out = subprocess.run(cmd, shell=True, cwd=cwd,
                             capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return ToolResult(False, f"timeout after {timeout}s")
    except Exception as e:
        return ToolResult(False, str(e))
    return ToolResult(True, {
        "stdout": out.stdout[-4000:],
        "stderr": out.stderr[-2000:],
        "exit": out.returncode,
    })


def tool_patch(path: str, old: str, new: str, **_) -> ToolResult:
    p = Path(path)
    if not p.exists():
        return ToolResult(False, f"path not found: {path}")
    text = p.read_text(encoding="utf-8", errors="replace")
    if old not in text:
        return ToolResult(False, "old string not found in file")
    if text.count(old) > 1:
        return ToolResult(False, f"old string matches {text.count(old)} times — make it unique")
    new_text = text.replace(old, new, 1)
    p.write_text(new_text, encoding="utf-8")
    return ToolResult(True, {"bytes": len(new_text)})


def tool_done(summary: str = "", **_) -> ToolResult:
    return ToolResult(True, {"summary": summary, "done": True})


TOOLS = {
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "search": tool_search,
    "run": tool_run,
    "patch": tool_patch,
    "done": tool_done,
}


# ---------------------------------------------------------------------------
# LLM CALL
# ---------------------------------------------------------------------------

def call_openrouter(messages: list[dict], model: str) -> str:
    """OpenAI-format chat completion. Works with OpenRouter, 9Router, any OpenAI-compatible gateway.

    Reads config from env:
      OPENROUTER_URL  (default: https://openrouter.ai/api/v1/chat/completions)
      OPENROUTER_API_KEY  (default: empty -- ok for localhost gateways)
    Handles both JSON and streaming (data: ...) responses automatically.
    """
    url = os.environ.get("OPENROUTER_URL", OPENROUTER_URL)
    key = os.environ.get("OPENROUTER_API_KEY", "")
    import urllib.request
    headers = {"content-type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
        headers["HTTP-Referer"] = "https://github.com/sisyphusse1-ops/gemma-coder"
        headers["X-Title"] = "gemma-coder"
    body = {"model": model, "messages": messages, "temperature": 0.2, "stream": False}
    req = urllib.request.Request(url, method="POST", headers=headers,
                                 data=json.dumps(body).encode())
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read().decode("utf-8", errors="replace")
    # normal JSON path
    try:
        parsed = json.loads(raw)
        return parsed["choices"][0]["message"]["content"]
    except json.JSONDecodeError:
        pass
    # streaming SSE fallback — concatenate deltas
    parts: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            ev = json.loads(payload)
            ch = ev.get("choices", [{}])[0]
            delta = ch.get("delta", {}).get("content") or ch.get("message", {}).get("content")
            if delta:
                parts.append(delta)
        except Exception:
            continue
    return "".join(parts)


def call_ollama(messages: list[dict], model: str) -> str:
    import urllib.request
    payload = {"model": model, "messages": messages, "stream": False}
    req = urllib.request.Request(OLLAMA_URL, method="POST",
                                 headers={"content-type": "application/json"},
                                 data=json.dumps(payload).encode())
    with urllib.request.urlopen(req, timeout=180) as r:
        body = json.loads(r.read())
    return body["message"]["content"]


def call_llm(messages: list[dict], provider: str, model: str) -> str:
    if provider == "openrouter":
        return call_openrouter(messages, model)
    if provider == "ollama":
        return call_ollama(messages, model)
    raise ValueError(f"unknown provider: {provider}")


# ---------------------------------------------------------------------------
# TOOL EXTRACTION
# ---------------------------------------------------------------------------

TOOL_RE = re.compile(r"<tool>\s*(\{.*?\})\s*</tool>", re.DOTALL)


def extract_tool_call(reply: str) -> dict | None:
    matches = TOOL_RE.findall(reply)
    if not matches:
        return None
    try:
        return json.loads(matches[-1])
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# AGENT LOOP
# ---------------------------------------------------------------------------

def load_rulebook(cwd: Path) -> str:
    for candidate in ("CLAUDE.md", "AGENTS.md", ".cursorrules"):
        p = cwd / candidate
        if p.exists():
            return p.read_text(encoding="utf-8", errors="replace")
    return "(no project rulebook found — fall back to general good practices)"


def run_agent(task: str, cwd: Path, provider: str, model: str,
              max_iter: int = MAX_ITER, verbose: bool = True) -> int:
    rulebook = load_rulebook(cwd)
    system = SYSTEM_PROMPT_TEMPLATE.format(
        tool_schema=TOOL_SCHEMA,
        rulebook=rulebook[:8000],
        cwd=str(cwd),
        task=task,
    )
    messages = [{"role": "system", "content": system}]
    messages.append({"role": "user", "content": task})

    for step in range(1, max_iter + 1):
        if verbose:
            print(f"\n━━━ step {step}/{max_iter} ━━━", flush=True)
        reply = call_llm(messages, provider, model)
        if verbose:
            # show reasoning (non-tool prose)
            clean = TOOL_RE.sub("", reply).strip()
            if clean:
                print(clean[:500], flush=True)

        call = extract_tool_call(reply)
        if not call:
            if verbose:
                print("(no tool call — ending)", flush=True)
            messages.append({"role": "assistant", "content": reply})
            return 0

        name = call.get("name")
        args = call.get("args", {})

        if verbose:
            print(f"→ tool: {name}({json.dumps(args)[:200]})", flush=True)

        if name not in TOOLS:
            result = ToolResult(False, f"unknown tool: {name}")
        else:
            try:
                result = TOOLS[name](**args)
            except TypeError as e:
                result = ToolResult(False, f"arg error: {e}")
            except Exception as e:
                result = ToolResult(False, f"tool error: {e}")

        if verbose:
            print(f"← {str(result.value)[:300]}", flush=True)

        messages.append({"role": "assistant", "content": reply})
        messages.append({"role": "user",
                         "content": f"<tool_result>{json.dumps(result.to_dict())[:4000]}</tool_result>"})

        if name == "done":
            return 0

    print("\n⚠ max iterations reached — stopping.")
    return 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("task", nargs="*", help="task description")
    p.add_argument("--cwd", default=".", help="working directory (default: .)")
    p.add_argument("--provider", default=DEFAULT_PROVIDER, choices=["openrouter", "ollama"])
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--max-iter", type=int, default=MAX_ITER)
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args(argv)

    if not args.task:
        p.print_help()
        return 1

    task = " ".join(args.task)
    return run_agent(
        task=task,
        cwd=Path(args.cwd).resolve(),
        provider=args.provider,
        model=args.model,
        max_iter=args.max_iter,
        verbose=not args.quiet,
    )


if __name__ == "__main__":
    sys.exit(main())
