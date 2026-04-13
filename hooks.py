"""
hooks.py — Claude Code hook integration

Adds epistemix as a PreToolUse hook so it runs automatically
before Claude Code starts working on any task.
"""

import json
from pathlib import Path


HOOK_SCRIPT = """\
#!/bin/bash
# Epistemix pre-task hook
# Reads CLAUDE_TOOL_INPUT from stdin (Claude Code passes tool input as JSON)

INPUT=$(cat)
PROMPT=$(echo "$INPUT" | python3 -c "
import sys, json
data = json.load(sys.stdin)
# Claude Code passes the task/prompt in different fields depending on tool
prompt = data.get('prompt') or data.get('task') or data.get('description') or ''
print(prompt[:200])
" 2>/dev/null)

if [ -n "$PROMPT" ]; then
    epistemix verify "$PROMPT" --silent
fi
"""

SETTINGS_HOOK = {
    "matcher": "Task",
    "hooks": [
        {
            "type": "command",
            "command": "epistemix hook-run"
        }
    ]
}


def install_hook(project_root: Path) -> bool:
    """
    Add epistemix to Claude Code's PreToolUse hooks.
    Modifies .claude/settings.json (creates if missing).
    """
    settings_path = project_root / ".claude" / "settings.json"
    settings_path.parent.mkdir(exist_ok=True)

    # Load existing settings
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            settings = {}
    else:
        settings = {}

    # Add hook
    hooks = settings.setdefault("hooks", {})
    pre = hooks.setdefault("PreToolUse", [])

    # Don't add duplicate
    for existing in pre:
        if existing.get("matcher") == "Task":
            cmds = [h.get("command") for h in existing.get("hooks", [])]
            if "epistemix hook-run" in cmds:
                return False  # already installed

    pre.append(SETTINGS_HOOK)
    settings_path.write_text(json.dumps(settings, indent=2))
    return True
