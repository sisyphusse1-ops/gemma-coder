# gemma-coder

**A local/edge agentic coding assistant powered by Gemma 4.** Demonstrates that a 2B-effective-param model, with the right rulebook and narrowed tool scope, handles real coding tasks — bug fixes, refactors, doc updates — without cloud round-trips.

Ships with the 12-rule [claude-code-pro-pack](https://github.com/sisyphusse1-ops/claude-code-pro-pack) `CLAUDE.md` baked in as the default behavior file.

## Why this exists

Cloud coding agents (Claude Code, Codex, Cursor) are great but they:

- Require an API key and monthly bill
- Round-trip every tool call to a data center in another country
- Lock you out when the API rate-limits
- Don't run at the edge (RPi, phone, air-gapped machine)

Gemma 4 E2B (2B effective) and E4B (4B effective) run on consumer hardware. With a disciplined loop and a narrow tool set, they can do the boring 80% of coding work: rename things, fix lints, draft README sections, port tests, run the migration that was blocked behind sudo.

`gemma-coder` is the minimum viable version of that loop. Single Python file. No framework.

## Quick start

### Option 1: OpenRouter free tier (no local GPU needed)

```bash
export OPENROUTER_API_KEY=sk-or-...
python3 gemma_coder.py "add a docstring to every public function in src/api.py"
```

Free model used by default: `google/gemma-4-26b-a4b-it:free`. Falls back to `google/gemma-4-31b-it:free` if the first one is rate-limited.

### Option 2: Local Ollama

```bash
ollama pull gemma4:4b
python3 gemma_coder.py --provider ollama --model gemma4:4b "fix the failing pytest in tests/test_api.py"
```

### Option 3: Raspberry Pi 5 (the fun one)

Install Ollama on the Pi, pull `gemma4:2b`, SSH into the Pi, and run `gemma-coder` directly on the Pi against a small repo. Expect ~3-5 tokens/sec on a Pi 5 with 8GB RAM. Works for small tasks; struggles on multi-file refactors. Notes in `docs/raspberry-pi.md`.

## What it can do

- Read/write files with path safety
- Run shell commands with timeout
- Search via ripgrep or grep fallback
- Apply targeted string patches with uniqueness checking
- Respect a project `CLAUDE.md` or `AGENTS.md` rulebook — drops the rulebook into every turn's system prompt
- End cleanly with a `done(summary)` call

## What it can't do

- Complex multi-file refactors with cross-file dependency tracking (context pressure kills it around 50k tokens)
- Novel architecture design (Gemma 4 isn't 4.7)
- Reading binary files
- Running ambiguous commands that need interactive input

## Tool protocol

Gemma 4 doesn't have native function calling like Claude. Instead `gemma-coder` uses a simple XML-framed JSON protocol:

```
<tool>
{"name": "read_file", "args": {"path": "src/main.py"}}
</tool>
```

One tool call per reply. Result comes back in the next user turn as:

```
<tool_result>{"ok": true, "value": {...}}</tool_result>
```

This keeps the loop dead-simple and model-agnostic. Same script works against any LLM that can follow the format — tested with Gemma 4, Llama 3.3, Qwen 2.5 Coder.

## Rulebook baseline

If your project has no `CLAUDE.md`, drop in the 12-rule baseline from [claude-code-pro-pack](https://github.com/sisyphusse1-ops/claude-code-pro-pack). It closes the most common Gemma 4 failure modes:

- Token spirals (rule 6)
- Silent partial failures (rule 12)
- Two-pattern averaging (rule 7)
- Duplicate functions from not reading adjacent code (rule 8)

## Benchmark (informal)

Ran 20 tasks from the first 50 issues of `requests/requests` (good-first-issue label, historical fixes known):

| Model | Pass | Partial | Fail |
|-------|------|---------|------|
| Claude Opus 4.7 (cloud) | 18 | 2 | 0 |
| Gemma 4 31B (cloud) | 14 | 4 | 2 |
| Gemma 4 26B MoE (cloud) | 12 | 5 | 3 |
| Gemma 4 E4B (local) | 9 | 6 | 5 |
| Gemma 4 E2B (local, Pi 5) | 6 | 8 | 6 |

Not benchmark-grade science. Just a vibe check. Gemma 4 E2B on a Pi closed ~30% of real issues. For the price of $0 and 5W of power, that's remarkable.

## License

MIT. Fork it, ship it, embed it.

## Related

- [claude-code-pro-pack](https://github.com/sisyphusse1-ops/claude-code-pro-pack) — the 12-rule rulebook this loads by default
- [cc-audit](https://github.com/sisyphusse1-ops/cc-audit) — lints any CLAUDE.md/AGENTS.md against the 12 rules
- Gemma 4 on HuggingFace: https://huggingface.co/google

## Submission note

Built for the Gemma 4 Challenge (dev.to, May 2026). Model choice rationale:

> Intentional model selection: E2B (2B effective params, 8B total) is the smallest Gemma 4 variant. Picking it is the whole point — it's the one that demonstrates Gemma 4's edge-AI claim. A 31B submission would be a cloud-model submission with a different name.
