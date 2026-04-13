"""
cli.py — Epistemix CLI
"""

import sys
import json
from pathlib import Path

from .scanner import extract_task_terms, score_files, generate_verification_questions, build_context_for_claude
from .priors import init_db, get_priors_for_terms, get_all_priors, save_prior, record_calibration, calibration_report
from .installer import setup_project


def sep(char="─", w=60):
    print(char * w)


def find_project_root() -> Path:
    current = Path.cwd()
    for parent in [current] + list(current.parents):
        if (parent / ".git").exists() or (parent / "CLAUDE.md").exists():
            return parent
    return current


def cmd_init(args):
    force = "--force" in args
    root = find_project_root()
    print(f"\n🔧 Setting up Epistemix in: {root}\n")
    results = setup_project(root, force=force)
    status = {True: "✅ created", False: "⏭  already exists"}
    print(f"  {status[results['epistemix_dir']]}  .epistemix/")
    print(f"  {status[results['context']]}  .epistemix/context.md")
    print(f"  {status[results['slash_command']]}  .claude/commands/verify.md")
    print(f"  {status[results['claude_md']]}  CLAUDE.md")
    print(f"  {status[results['gitignore']]}  .gitignore")
    print()
    sep("═")
    print("\n✅ Epistemix ready. Next: epistemix mcp-install (recommended)\n")
    sep("═")


def cmd_verify(args):
    if not args:
        print("Usage: epistemix verify \"task description\"")
        sys.exit(1)
    task = " ".join(a for a in args if not a.startswith("--"))
    root = find_project_root()
    silent = "--silent" in args

    if not silent:
        print(f"\n🔍 Analyzing: {task}\n")

    terms = extract_task_terms(task)
    conn = init_db(root)
    existing = get_priors_for_terms(conn, terms)
    scored = score_files(root, terms)
    questions = generate_verification_questions(scored, terms)
    context = build_context_for_claude(task, scored, questions, existing)

    context_path = root / ".epistemix" / "context.md"
    context_path.parent.mkdir(exist_ok=True)
    context_path.write_text(context)

    if not silent:
        sep()
        print("📊 ANALYSIS SUMMARY")
        sep()
        print("  Top files by VoI:")
        for fs in scored[:5]:
            print(f"    {fs.path.name:40s} VoI={fs.voi:.1f}")
        if existing:
            print(f"\n  Known priors ({len(existing)}):")
            for p in existing:
                print(f"    ✅ {p['term']} = {p['definition']}")
        if questions:
            print(f"\n  Verification questions ({len(questions)}):")
            for q in questions[:3]:
                print(f"    ❓ {q.question}")
        print()
        sep("═")
        print(f"\n✅ Context → .epistemix/context.md")
        print(f"   In Claude Code: /verify {task}\n")
        sep("═")


def cmd_priors(args):
    root = find_project_root()
    conn = init_db(root)
    priors = get_all_priors(conn)
    if not priors:
        print("\n📭 No priors yet.\n")
        return
    print(f"\n📚 Project priors ({len(priors)} terms)\n")
    sep()
    for p in priors:
        print(f"  {p['term']:30s} {p['definition']}")
        print(f"  {'':30s} _{p['confirmed_at'][:10]}_\n")


def cmd_calibrate(args):
    if not args or args[0] not in ("yes", "no", "1", "0"):
        print("Usage: epistemix calibrate yes|no")
        sys.exit(1)
    success = args[0] in ("yes", "1")
    root = find_project_root()
    conn = init_db(root)
    context_path = root / ".epistemix" / "context.md"
    task = "unknown"
    if context_path.exists():
        for line in context_path.read_text().splitlines():
            if line.strip() and not line.startswith("#"):
                task = line.strip()
                break
    record_calibration(conn, task, 0.8, success)
    print(f"\n  Recorded: {'✅ Success' if success else '❌ Failed'}\n")


def cmd_calibration_report(args):
    root = find_project_root()
    conn = init_db(root)
    report = calibration_report(conn)
    if "error" in report:
        print(f"\n⚠️  {report['error']}\n")
        return
    print(f"\n📈 Calibration ({report['n_tasks']} tasks)")
    sep()
    print(f"  ECE: {report['ece']}  →  {report['interpretation']}\n")
    for bucket, stats in sorted(report["buckets"].items()):
        gap = f"+{stats['gap']:.2f}" if stats["gap"] >= 0 else f"{stats['gap']:.2f}"
        print(f"  {bucket:12s} {'█'*stats['n']:10s} predicted={stats['predicted']:.0%} actual={stats['actual']:.0%} gap={gap}")
    print()


def cmd_mcp_install(args):
    from .mcp_server import install_mcp
    root = find_project_root()
    installed = install_mcp(root)
    if installed:
        print(f"\n✅ MCP server registered in .claude/mcp_servers.json")
        print("   Restart Claude Code — epistemix tools will be available natively.\n")
        print("   Tools Claude can call:")
        print("     epistemix_analyze(task)    — VoI analysis + verification questions")
        print("     epistemix_prior_get(term)  — look up confirmed definition")
        print("     epistemix_prior_set(...)   — save confirmed term")
        print("     epistemix_priors()         — list all project knowledge\n")
    else:
        print("\n⏭  Already registered.\n")


def cmd_hook_install(args):
    from .hooks import install_hook
    root = find_project_root()
    installed = install_hook(root)
    if installed:
        print(f"\n✅ PreToolUse hook added to .claude/settings.json\n")
    else:
        print("\n⏭  Hook already installed.\n")


def cmd_mcp_server(args):
    from .mcp_server import run_server
    run_server()


def cmd_help():
    print("""
Epistemix — epistemic verification for Claude Code

Setup (run once per project):
  epistemix init              CLAUDE.md + /verify slash command
  epistemix mcp-install       Native MCP tools (recommended)
  epistemix hook-install      Auto-trigger on every task

Usage:
  epistemix verify "task"     Analyze task → .epistemix/context.md
  epistemix priors            Show confirmed project knowledge
  epistemix calibrate yes|no  Record task outcome
  epistemix calibration       Show calibration report (ECE)

Integration levels:
  1. Slash command:  /verify task in Claude Code
  2. Auto hook:      runs before every Claude Code task
  3. MCP server:     Claude calls epistemix_analyze() as a native tool
""")


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("help", "--help", "-h"):
        cmd_help()
    elif args[0] == "init":
        cmd_init(args[1:])
    elif args[0] == "verify":
        cmd_verify(args[1:])
    elif args[0] == "priors":
        cmd_priors(args[1:])
    elif args[0] == "calibrate":
        cmd_calibrate(args[1:])
    elif args[0] == "calibration":
        cmd_calibration_report(args[1:])
    elif args[0] == "mcp-install":
        cmd_mcp_install(args[1:])
    elif args[0] == "hook-install":
        cmd_hook_install(args[1:])
    elif args[0] == "mcp-server":
        cmd_mcp_server(args[1:])
    else:
        print(f"Unknown command: {args[0]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
