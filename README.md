# Epistemix

**Epistemic verification for Claude Code.**

Before Claude writes a single line of code, Epistemix forces it to expose its assumptions about your codebase — and verifies them against the actual code.

---

## The problem

Claude Code doesn't know what it doesn't know. It will implement your task based on what it *thinks* it understands, without telling you it's guessing.

```
You:    "Add report download for premium users"
Claude: [assumes premium = is_premium flag in DB]
        [implements it wrong, tests pass]
You:    [discovers the bug in production]
```

## How Epistemix fixes this

1. Scores your files by **Value of Information** for the task — tells Claude what to read first
2. Generates **verification questions with known answers** from your AST — no self-report
3. Forces Claude to **expose every assumption** before touching any code
4. Stores confirmed definitions as **persistent priors** — no re-verification across sessions

---

## Install

```bash
pip install epistemix
```

Zero external dependencies. Pure Python stdlib.

---

## Setup (once per project)

```bash
cd your-project
epistemix init          # CLAUDE.md + /verify slash command
epistemix mcp-install   # recommended: native MCP tools in Claude Code
```

Restart Claude Code after `mcp-install`.

---

## Usage

```bash
# Before every task
epistemix verify "add report download for premium users"

# After task completes
epistemix calibrate yes   # or: no
```

### In Claude Code — three integration levels

**1. Slash command** (manual, always available):
```
/verify add report download for premium users
```

**2. MCP tools** (automatic, after `epistemix mcp-install`):

Claude calls these as native tools mid-conversation:
- `epistemix_analyze(task)` — VoI analysis + AST verification questions
- `epistemix_prior_get(term)` — look up confirmed definition
- `epistemix_prior_set(term, definition)` — save confirmed term
- `epistemix_priors()` — list all project knowledge

**3. Auto hook** (after `epistemix hook-install`):

Runs automatically before every Claude Code task via `PreToolUse`.

---

## All commands

```
epistemix init              CLAUDE.md + /verify slash command
epistemix init --force      Overwrite existing files

epistemix mcp-install       Register as MCP server (recommended)
epistemix hook-install      Add PreToolUse hook (auto-trigger)

epistemix verify "task"     Analyze task → .epistemix/context.md
epistemix priors            Show all confirmed project knowledge
epistemix calibrate yes|no  Record task outcome
epistemix calibration       Show calibration report (ECE)
```

---

## What gets created in your project

```
your-project/
  CLAUDE.md                     Protocol instructions for Claude
  .claude/
    commands/verify.md          /verify slash command
    mcp_servers.json            MCP registration (after mcp-install)
    settings.json               Hook config (after hook-install)
  .epistemix/                   gitignored — runtime only
    context.md                  Pre-task analysis (updated by verify)
    priors.db                   SQLite: confirmed terms + calibration history
```

---

## Supported stacks

TypeScript / Next.js, C# / .NET, Python, JavaScript / React

---

## How it differs from Empirica

| | Empirica | Epistemix |
|---|---|---|
| Measures | Self-report (0–100%) | AST questions with known answers |
| File selection | Keyword frequency | Value of Information scoring |
| Priors | Resets each session | SQLite — persists across sessions |
| Dependencies | Many | Zero |

---

## License

MIT
