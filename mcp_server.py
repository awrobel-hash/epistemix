#!/usr/bin/env python3
"""
mcp_server.py — Epistemix MCP server

Exposes epistemix as native Claude Code tools.
Claude can call these directly mid-conversation.

Install:
    epistemix mcp-install

This adds epistemix to .claude/mcp_servers.json so Claude Code
discovers it automatically.

Tools exposed:
    epistemix_analyze   — VoI analysis + verification questions for a task
    epistemix_prior_get — Look up a confirmed term definition
    epistemix_prior_set — Save a confirmed term definition
    epistemix_priors    — List all known priors
"""

import sys
import json
from pathlib import Path


def handle_request(request: dict) -> dict:
    """Route MCP JSON-RPC requests to the right handler."""

    method = request.get("method", "")
    params = request.get("params", {})
    req_id = request.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "epistemix", "version": "0.1.0"},
            }
        }

    if method == "tools/list":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {"tools": TOOLS}
        }

    if method == "tools/call":
        tool_name = params.get("name")
        args = params.get("arguments", {})
        result = dispatch_tool(tool_name, args)
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "content": [{"type": "text", "text": result}]
            }
        }

    return {
        "jsonrpc": "2.0", "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"}
    }


TOOLS = [
    {
        "name": "epistemix_analyze",
        "description": (
            "Analyze a task before implementing it. "
            "Returns: top files to read (ranked by Value of Information), "
            "auto-generated verification questions with known answers from AST, "
            "and any existing priors for task terms. "
            "Call this BEFORE reading any files or writing any code."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The task description to analyze"
                }
            },
            "required": ["task"]
        }
    },
    {
        "name": "epistemix_prior_get",
        "description": "Look up a confirmed definition for a domain term.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "term": {"type": "string", "description": "The term to look up"}
            },
            "required": ["term"]
        }
    },
    {
        "name": "epistemix_prior_set",
        "description": (
            "Save a confirmed term definition after the user has verified it. "
            "Call this when the user confirms how a domain term should be interpreted."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "term": {"type": "string"},
                "definition": {"type": "string"},
                "source": {"type": "string", "description": "file:line where found"}
            },
            "required": ["term", "definition"]
        }
    },
    {
        "name": "epistemix_priors",
        "description": "List all confirmed priors for this project.",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    }
]


def dispatch_tool(name: str, args: dict) -> str:
    """Call the right epistemix function and return result as text."""
    from pathlib import Path
    import os

    project_root = Path(os.environ.get("EPISTEMIX_ROOT", Path.cwd()))

    try:
        if name == "epistemix_analyze":
            return tool_analyze(args["task"], project_root)
        elif name == "epistemix_prior_get":
            return tool_prior_get(args["term"], project_root)
        elif name == "epistemix_prior_set":
            return tool_prior_set(
                args["term"], args["definition"],
                args.get("source", ""), project_root
            )
        elif name == "epistemix_priors":
            return tool_priors(project_root)
        else:
            return f"Unknown tool: {name}"
    except Exception as e:
        return f"Error: {e}"


def tool_analyze(task: str, root: Path) -> str:
    from .scanner import extract_task_terms, score_files, generate_verification_questions
    from .priors import init_db, get_priors_for_terms

    terms = extract_task_terms(task)
    conn = init_db(root)
    existing = get_priors_for_terms(conn, terms)
    scored = score_files(root, terms)
    questions = generate_verification_questions(scored, terms)

    lines = [f"# Epistemix analysis: {task}", ""]

    if existing:
        lines += ["## Known priors (skip verification for these)", ""]
        for p in existing:
            lines.append(f"- **{p['term']}** = {p['definition']}")
        lines.append("")

    lines += ["## Read these files first (ranked by VoI)", ""]
    for fs in scored[:6]:
        lines.append(f"- `{fs.path.name}` — VoI={fs.voi:.1f}")
        if fs.entities:
            names = ", ".join(f"`{e.name}`" for e in fs.entities[:4])
            lines.append(f"  Contains: {names}")
    lines.append("")

    if questions:
        lines += ["## Verification questions (find answers in code before implementing)", ""]
        for i, q in enumerate(questions, 1):
            lines.append(f"{i}. {q.question} _(look in {q.source})_")
        lines.append("")

    lines += [
        "## Next steps",
        "1. Read the files above",
        "2. Answer each verification question with file:line",
        "3. Present CERTAIN / UNCERTAIN / MISSING to the user",
        "4. Call `epistemix_prior_set` for each confirmed term",
        "5. Only then implement"
    ]

    return "\n".join(lines)


def tool_prior_get(term: str, root: Path) -> str:
    from .priors import init_db, get_prior
    conn = init_db(root)
    prior = get_prior(conn, term)
    if prior:
        return f"**{term}** = {prior['definition']} _(confirmed {prior['confirmed_at'][:10]})_"
    return f"No prior found for '{term}'. Ask the user to define it."


def tool_prior_set(term: str, definition: str, source: str, root: Path) -> str:
    from .priors import init_db, save_prior
    conn = init_db(root)
    save_prior(conn, term, definition, source=source)
    return f"Saved: **{term}** = {definition}"


def tool_priors(root: Path) -> str:
    from .priors import init_db, get_all_priors
    conn = init_db(root)
    priors = get_all_priors(conn)
    if not priors:
        return "No priors yet."
    lines = ["# Project priors", ""]
    for p in priors:
        lines.append(f"- **{p['term']}**: {p['definition']}")
    return "\n".join(lines)


def run_server() -> None:
    """stdio MCP server loop."""
    import os
    os.environ.setdefault("EPISTEMIX_ROOT", str(Path.cwd()))

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            response = handle_request(request)
        except json.JSONDecodeError:
            response = {
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": "Parse error"}
            }
        print(json.dumps(response), flush=True)


def install_mcp(project_root: Path) -> bool:
    """
    Register epistemix MCP server in .claude/mcp_servers.json
    so Claude Code discovers it automatically.
    """
    import shutil

    mcp_path = project_root / ".claude" / "mcp_servers.json"
    mcp_path.parent.mkdir(exist_ok=True)

    if mcp_path.exists():
        try:
            config = json.loads(mcp_path.read_text())
        except json.JSONDecodeError:
            config = {}
    else:
        config = {}

    servers = config.setdefault("mcpServers", {})

    if "epistemix" in servers:
        return False  # already installed

    epistemix_bin = shutil.which("epistemix")
    if not epistemix_bin:
        epistemix_bin = "epistemix"

    servers["epistemix"] = {
        "command": epistemix_bin,
        "args": ["mcp-server"],
        "env": {
            "EPISTEMIX_ROOT": str(project_root)
        }
    }

    mcp_path.write_text(json.dumps(config, indent=2))
    return True


if __name__ == "__main__":
    run_server()
