"""
scanner.py — VoI-driven codebase analysis

Extracts entities from code using language-specific patterns,
scores files by Value of Information for a given task,
and generates verification questions from AST facts.
"""

import re
import ast
import math
import sqlite3
from pathlib import Path
from dataclasses import dataclass, field


SKIP_DIRS = {
    "node_modules", ".git", "bin", "obj", ".next", "dist", "build",
    "__pycache__", ".pytest_cache", ".epistemix", "migrations",
}

SCAN_EXTENSIONS = {
    ".ts", ".tsx", ".js", ".jsx",
    ".cs",
    ".py",
    ".json", ".md", ".yaml", ".yml",
}


@dataclass
class CodeEntity:
    """A named entity extracted from code."""
    name: str
    kind: str           # class, function, interface, method, type, namespace
    file: Path
    line: int
    signature: str = ""  # for functions: param list; for classes: base classes


@dataclass
class FileScore:
    """VoI score for a file relative to a task."""
    path: Path
    relevance: float        # how many task terms appear
    centrality: float       # how many other files import this
    entity_count: int       # number of named entities
    voi: float              # combined score
    content: str = field(default="", repr=False)
    entities: list[CodeEntity] = field(default_factory=list)


# ── Language-specific entity extractors ──────────────────────────────────────

def extract_entities_python(content: str, path: Path) -> list[CodeEntity]:
    entities = []
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            bases = [ast.unparse(b) for b in node.bases]
            entities.append(CodeEntity(
                name=node.name, kind="class", file=path, line=node.lineno,
                signature=", ".join(bases)
            ))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [a.arg for a in node.args.args]
            entities.append(CodeEntity(
                name=node.name, kind="function", file=path, line=node.lineno,
                signature=", ".join(args)
            ))
    return entities


def extract_entities_typescript(content: str, path: Path) -> list[CodeEntity]:
    entities = []

    patterns = [
        (r"(?:export\s+)?(?:abstract\s+)?class\s+(\w+)(?:\s+extends\s+(\w+))?(?:\s+implements\s+([\w,\s]+))?", "class"),
        (r"(?:export\s+)?interface\s+(\w+)(?:\s+extends\s+([\w,\s]+))?", "interface"),
        (r"(?:export\s+)?type\s+(\w+)\s*=", "type"),
        (r"(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)", "function"),
        (r"(?:const|let)\s+(\w+)\s*=\s*(?:async\s+)?\(([^)]*)\)\s*(?::\s*[\w<>[\]]+)?\s*=>", "function"),
        (r"(?:public|private|protected|static|async)?\s*(\w+)\s*\(([^)]*)\)\s*(?::\s*[\w<>[\]|&\s]+)?\s*\{", "method"),
        (r"(?:export\s+)?enum\s+(\w+)", "enum"),
    ]

    for line_no, line in enumerate(content.splitlines(), 1):
        for pattern, kind in patterns:
            m = re.search(pattern, line)
            if m and m.group(1) and m.group(1) not in ("if", "for", "while", "switch", "catch"):
                entities.append(CodeEntity(
                    name=m.group(1), kind=kind, file=path, line=line_no,
                    signature=m.group(2) if m.lastindex and m.lastindex >= 2 else ""
                ))
    return entities


def extract_entities_csharp(content: str, path: Path) -> list[CodeEntity]:
    entities = []

    patterns = [
        (r"(?:public|internal|private|protected)?\s*(?:abstract|sealed|static)?\s*class\s+(\w+)(?:\s*:\s*([\w,\s<>]+))?", "class"),
        (r"(?:public|internal)?\s*interface\s+(\w+)(?:\s*:\s*([\w,\s<>]+))?", "interface"),
        (r"namespace\s+([\w.]+)", "namespace"),
        (r"(?:public|private|protected|internal)(?:\s+static)?(?:\s+async)?\s+[\w<>\[\]]+\s+(\w+)\s*\(([^)]*)\)", "method"),
        (r"(?:public|internal)?\s*(?:sealed|abstract)?\s*record\s+(\w+)", "record"),
        (r"(?:public|internal)?\s*enum\s+(\w+)", "enum"),
    ]

    for line_no, line in enumerate(content.splitlines(), 1):
        for pattern, kind in patterns:
            m = re.search(pattern, line)
            if m and m.group(1):
                entities.append(CodeEntity(
                    name=m.group(1), kind=kind, file=path, line=line_no,
                    signature=m.group(2) if m.lastindex and m.lastindex >= 2 else ""
                ))
    return entities


def extract_entities(content: str, path: Path) -> list[CodeEntity]:
    suffix = path.suffix.lower()
    if suffix == ".py":
        return extract_entities_python(content, path)
    elif suffix in (".ts", ".tsx", ".js", ".jsx"):
        return extract_entities_typescript(content, path)
    elif suffix == ".cs":
        return extract_entities_csharp(content, path)
    return []


# ── Import graph for centrality ───────────────────────────────────────────────

def build_import_graph(files: list[Path]) -> dict[str, int]:
    """Count how many files import each filename stem (proxy for centrality)."""
    import_counts: dict[str, int] = {}
    name_set = {f.stem.lower() for f in files}

    for path in files:
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        # TypeScript / JS imports
        for m in re.finditer(r"""(?:import|from|require)\s*['"(]([^'"()]+)['")]""", content):
            stem = Path(m.group(1)).stem.lower().lstrip("./")
            if stem in name_set:
                import_counts[stem] = import_counts.get(stem, 0) + 1

        # C# using
        for m in re.finditer(r"using\s+([\w.]+);", content):
            last = m.group(1).split(".")[-1].lower()
            if last in name_set:
                import_counts[last] = import_counts.get(last, 0) + 1

        # Python imports
        for m in re.finditer(r"(?:from|import)\s+([\w.]+)", content):
            last = m.group(1).split(".")[-1].lower()
            if last in name_set:
                import_counts[last] = import_counts.get(last, 0) + 1

    return import_counts


# ── VoI scoring ───────────────────────────────────────────────────────────────

def score_files(root: Path, task_terms: set[str], max_files: int = 30) -> list[FileScore]:
    """
    Score all files by VoI for the given task.
    VoI = relevance × (1 + log(1 + centrality)) / log(1 + size_kb)
    """
    all_files = [
        p for p in root.rglob("*")
        if p.is_file()
        and p.suffix in SCAN_EXTENSIONS
        and not any(skip in p.parts for skip in SKIP_DIRS)
    ]

    import_counts = build_import_graph(all_files)

    scored: list[FileScore] = []

    for path in all_files:
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        content_lower = content.lower()
        name_lower = path.stem.lower()

        # Relevance: term hits in content + name bonus
        term_hits = sum(content_lower.count(t) for t in task_terms)
        name_hits = sum(1 for t in task_terms if t in name_lower) * 5
        relevance = term_hits + name_hits

        if relevance == 0:
            continue

        # Centrality: how many other files import this
        centrality = import_counts.get(name_lower, 0)

        # Size penalty (prefer focused files over huge ones)
        size_kb = max(len(content) / 1024, 0.1)

        voi = relevance * (1 + math.log1p(centrality)) / math.log1p(size_kb)

        entities = extract_entities(content, path)

        scored.append(FileScore(
            path=path,
            relevance=relevance,
            centrality=centrality,
            entity_count=len(entities),
            voi=voi,
            content=content[:4000],  # cap for context
            entities=entities,
        ))

    scored.sort(key=lambda x: -x.voi)
    return scored[:max_files]


# ── Task term extraction ──────────────────────────────────────────────────────

def extract_task_terms(task: str) -> set[str]:
    """Extract meaningful terms from task description."""
    # Remove common stop words
    stop = {
        "a", "an", "the", "and", "or", "for", "to", "of", "in",
        "is", "be", "add", "get", "set", "do", "make", "use",
        "with", "that", "this", "from", "as", "by", "on", "at",
        "it", "its", "can", "will", "should", "must", "need",
        "want", "when", "where", "how", "what", "which",
    }

    # Extract words + camelCase parts
    words = re.findall(r"[A-Za-z]{3,}", task)
    terms = set()
    for w in words:
        lower = w.lower()
        if lower not in stop:
            terms.add(lower)
        # Split camelCase
        parts = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)", w)
        for p in parts:
            if len(p) >= 3 and p.lower() not in stop:
                terms.add(p.lower())

    return terms


# ── Verification question generation ─────────────────────────────────────────

@dataclass
class VerificationQuestion:
    """An auto-generated question with a known answer from the codebase."""
    question: str
    answer: str           # ground truth from code
    source: str           # file:line
    term: str             # which task term this verifies


def generate_verification_questions(
    scored_files: list[FileScore],
    task_terms: set[str],
    max_questions: int = 10,
) -> list[VerificationQuestion]:
    """
    Generate objective questions from AST facts.
    These have known answers — no self-report needed.
    """
    questions: list[VerificationQuestion] = []
    seen_terms: set[str] = set()

    for fs in scored_files:
        for entity in fs.entities:
            # Only generate questions for entities related to task terms
            entity_lower = entity.name.lower()
            related_terms = [t for t in task_terms if t in entity_lower or entity_lower in t]

            if not related_terms:
                continue

            term = related_terms[0]
            if term in seen_terms:
                continue

            source = f"{fs.path.name}:{entity.line}"

            if entity.kind == "class":
                q = VerificationQuestion(
                    question=f"What is `{entity.name}` and what does it extend/implement?",
                    answer=f"`{entity.name}` is a class in {source}"
                           + (f", extends/implements: {entity.signature}" if entity.signature else ""),
                    source=source,
                    term=term,
                )
                questions.append(q)
                seen_terms.add(term)

            elif entity.kind in ("function", "method"):
                q = VerificationQuestion(
                    question=f"What parameters does `{entity.name}()` accept?",
                    answer=f"`{entity.name}` in {source} accepts: ({entity.signature or 'none'})",
                    source=source,
                    term=term,
                )
                questions.append(q)
                seen_terms.add(term)

            elif entity.kind == "interface":
                q = VerificationQuestion(
                    question=f"What is the interface `{entity.name}` and what does it extend?",
                    answer=f"`{entity.name}` is an interface in {source}"
                           + (f", extends: {entity.signature}" if entity.signature else ""),
                    source=source,
                    term=term,
                )
                questions.append(q)
                seen_terms.add(term)

            if len(questions) >= max_questions:
                break

        if len(questions) >= max_questions:
            break

    return questions


# ── Context builder ───────────────────────────────────────────────────────────

def build_context_for_claude(
    task: str,
    scored_files: list[FileScore],
    questions: list[VerificationQuestion],
    priors: list[dict],
) -> str:
    """
    Build the context string that goes into CLAUDE.md or is passed as context.
    This is what Claude Code will actually read.
    """
    lines = [
        "# Epistemix: Pre-task analysis",
        "",
        f"## Task\n{task}",
        "",
    ]

    if priors:
        lines += ["## Known priors (from previous sessions)", ""]
        for p in priors:
            lines.append(f"- **{p['term']}** = {p['definition']}")
            lines.append(f"  _(confirmed {p['confirmed_at']})_")
        lines.append("")

    lines += ["## Highest VoI files (read these first)", ""]
    for fs in scored_files[:8]:
        rel = fs.path.name
        lines.append(f"- `{rel}` — VoI: {fs.voi:.1f}, centrality: {fs.centrality}")
        if fs.entities:
            entity_names = ", ".join(f"`{e.name}`" for e in fs.entities[:5])
            lines.append(f"  Entities: {entity_names}")
    lines.append("")

    if questions:
        lines += [
            "## Verification questions (answer these from the code)",
            "_These have objective answers — find them before implementing._",
            "",
        ]
        for i, q in enumerate(questions, 1):
            lines.append(f"{i}. {q.question}")
            lines.append(f"   _(look in {q.source})_")
        lines.append("")

    lines += [
        "## Protocol",
        "1. Read the highest VoI files above",
        "2. Answer each verification question with a specific file:line reference",
        "3. Extract your assumptions about each domain term in the task",
        "4. Present: CERTAIN / UNCERTAIN / MISSING — wait for confirmation",
        "5. Only then implement",
    ]

    return "\n".join(lines)
