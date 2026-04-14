"""
scanner.py — VoI-driven codebase analysis

Extracts entities from code using language-specific patterns,
scores files by Value of Information for a given task,
and generates verification questions from AST facts.
"""

import re
import ast
import math

from pathlib import Path
from dataclasses import dataclass, field

try:
    from .type_graph import build_type_graph, TypeGraph
except ImportError:
    from type_graph import build_type_graph, TypeGraph  # type: ignore[no-redef]


SKIP_DIRS = {
    "node_modules", ".git", "bin", "obj", ".next", "dist", "build",
    "__pycache__", ".pytest_cache", ".epistemix", "migrations",
    "wwwroot", "packages", ".vs", ".idea", "coverage", "TestResults",
    ".nuget", "artifacts", "publish", "logs",
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
            # Detect if class inherits from Enum
            kind = "class"
            for b in node.bases:
                base_name = ast.unparse(b).lower()
                if "enum" in base_name:
                    kind = "enum"
                    break
            entities.append(CodeEntity(
                name=node.name, kind=kind, file=path, line=node.lineno,
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
    seen = set()  # avoid duplicates on same line

    patterns = [
        (r"(?:public|internal|private|protected)\s*(?:abstract|sealed|static|partial)?\s*(?:partial\s+)?class\s+(\w+)(?:<[^>]+>)?(?:\s*:\s*([\w,\s<>.]+))?", "class"),
        (r"(?:public|internal)\s*(?:partial\s+)?interface\s+(\w+)(?:<[^>]+>)?(?:\s*:\s*([\w,\s<>.]+))?", "interface"),
        (r"namespace\s+([\w.]+)", "namespace"),
        (r"(?:public|private|protected|internal)(?:\s+(?:static|virtual|override|abstract|async|new))*\s+[\w<>\[\]?]+\s+(\w+)\s*\(([^)]*)\)", "method"),
        (r"(?:public|internal)\s*(?:sealed\s+|abstract\s+)?(?:partial\s+)?record\s+(?:struct\s+|class\s+)?(\w+)", "record"),
        (r"(?:public|internal)\s*enum\s+(\w+)", "enum"),
        (r"(?:public|internal)\s*(?:static\s+)?class\s+(\w+)(?:Extensions|Helper|Factory|Builder|Handler|Service|Repository|Controller|Validator)", "class"),
    ]

    for line_no, line in enumerate(content.splitlines(), 1):
        for pattern, kind in patterns:
            m = re.search(pattern, line)
            if m and m.group(1):
                name = m.group(1)
                key = f"{name}:{line_no}"
                if key in seen:
                    continue
                seen.add(key)
                # Skip common noise
                if name in ("if", "for", "while", "switch", "catch", "get", "set", "value"):
                    continue
                entities.append(CodeEntity(
                    name=name, kind=kind, file=path, line=line_no,
                    signature=m.group(2).strip() if m.lastindex and m.lastindex >= 2 and m.group(2) else ""
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
    """Count how many files import each filename stem (proxy for centrality).

    For C#: also maps namespace segments to file stems, since C# uses
    'using Namespace.SubNamespace;' not 'import ./filename'.
    """
    import_counts: dict[str, int] = {}
    name_set = {f.stem.lower() for f in files}

    # For C#: build a map from namespace last-segment -> file stems in that dir
    # e.g., "Models" dir containing FilmAirline.cs -> all files in Models/ get credit
    dir_to_stems: dict[str, set[str]] = {}
    for f in files:
        if f.suffix == ".cs":
            parent_name = f.parent.name.lower()
            if parent_name not in dir_to_stems:
                dir_to_stems[parent_name] = set()
            dir_to_stems[parent_name].add(f.stem.lower())

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

        # C# using — match last segment AND all files in matching directories
        for m in re.finditer(r"using\s+([\w.]+);", content):
            parts = m.group(1).split(".")
            for part in parts:
                part_lower = part.lower()
                # Direct stem match
                if part_lower in name_set:
                    import_counts[part_lower] = import_counts.get(part_lower, 0) + 1
                # Directory match — credit all files in that directory
                if part_lower in dir_to_stems:
                    for stem in dir_to_stems[part_lower]:
                        if stem != path.stem.lower():  # don't self-credit
                            import_counts[stem] = import_counts.get(stem, 0) + 1

        # Python imports
        for m in re.finditer(r"(?:from|import)\s+([\w.]+)", content):
            last = m.group(1).split(".")[-1].lower()
            if last in name_set:
                import_counts[last] = import_counts.get(last, 0) + 1

    return import_counts


# ── Entity Index ──────────────────────────────────────────────────────────────

def build_entity_index(root: Path) -> dict[str, list[tuple[Path, int]]]:
    """Pre-scan ALL code files and index every class/interface/enum by name.

    Returns: {entity_name_lower: [(file_path, line_number), ...]}
    This is fast (~2s for 4000 files) because it only does regex, no full parse.
    """
    index: dict[str, list[tuple[Path, int]]] = {}
    test_dirs = {"tests", "test", "Tests", "Test", "__tests__", "spec", "Specs"}
    skip_all = SKIP_DIRS | test_dirs

    patterns = [
        re.compile(r"(?:public|internal|private)?\s*(?:abstract|sealed|static|partial)?\s*(?:partial\s+)?class\s+(\w+)"),
        re.compile(r"(?:public|internal)?\s*interface\s+(\w+)"),
        re.compile(r"(?:public|internal)?\s*enum\s+(\w+)"),
        re.compile(r"(?:public|internal)?\s*record\s+(\w+)"),
        re.compile(r"class\s+(\w+)"),  # Python
    ]

    code_exts = {".cs", ".py", ".ts", ".tsx", ".js", ".jsx"}

    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in code_exts:
            continue
        if any(s in path.parts for s in skip_all):
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        for line_no, line in enumerate(content.splitlines(), 1):
            for pat in patterns:
                m = pat.search(line)
                if m and m.group(1) and m.group(1) not in ("if", "for", "while", "get", "set"):
                    name = m.group(1).lower()
                    if name not in index:
                        index[name] = []
                    index[name].append((path, line_no))
                    break  # one match per line

    return index


# ── VoI scoring ───────────────────────────────────────────────────────────────

CODE_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx", ".cs", ".py"}

# Module-level cache for the type graph (built once per score_files call)
_cached_type_graph: TypeGraph | None = None


def score_files(root: Path, task_terms: set[str], max_files: int = 0) -> list[FileScore]:
    """
    Score all files by VoI for the given task.
    VoI = relevance * (1 + log(1 + centrality)) / log(1 + size_kb)

    Optimized for large codebases (4000+ files):
    - Only scans code files (not json/md/yaml) for entity extraction
    - Skips test directories for VoI scoring (tests are rarely the right first read)
    - Caps content reading at 8KB for VoI scoring
    """
    test_dirs = {"tests", "test", "Tests", "Test", "__tests__", "spec", "Specs"}
    skip_all = SKIP_DIRS | test_dirs

    all_files = [
        p for p in root.rglob("*")
        if p.is_file()
        and p.suffix in SCAN_EXTENSIONS
        and not any(skip in p.parts for skip in skip_all)
    ]

    # Build type reference graph for real centrality
    global _cached_type_graph
    type_graph = build_type_graph(root)
    _cached_type_graph = type_graph

    # Also keep legacy import graph as fallback for non-C# files
    code_files = [f for f in all_files if f.suffix in CODE_EXTENSIONS]
    import_counts = build_import_graph(code_files)

    scored: list[FileScore] = []

    # Two-phase scan for large codebases:
    # Phase 1: Quick path-based filter (fast — just checks filenames/paths)
    # Phase 2: Full content read only for files that pass filter
    n_files = len(all_files)
    term_doc_freq: dict[str, int] = {}
    file_contents: dict[str, tuple[str, str, list]] = {}

    # For large codebases (>500 files), pre-filter by filename/path
    if n_files > 500:
        # Only read files whose name or parent dir matches a task term
        candidate_files = []
        for path in all_files:
            path_str = "/".join(p.lower() for p in path.parts[-4:])
            name_lower = path.stem.lower()
            # Quick check: does filename or path contain any task term?
            if any(t in name_lower or t in path_str for t in task_terms):
                candidate_files.append(path)
            elif path.suffix in CODE_EXTENSIONS:
                # For code files, also include if parent dir is relevant
                candidate_files.append(path)
        all_files_to_read = candidate_files
    else:
        all_files_to_read = all_files

    for path in all_files_to_read:
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        content_lower = content.lower()
        entities = extract_entities(content, path)
        file_contents[str(path)] = (content, content_lower, entities)

        for t in task_terms:
            if t in content_lower:
                term_doc_freq[t] = term_doc_freq.get(t, 0) + 1

    for path in all_files:
        key = str(path)
        if key not in file_contents:
            continue
        content, content_lower, entities = file_contents[key]
        name_lower = path.stem.lower()

        # TF-IDF relevance: terms that appear in fewer files get higher weight
        tfidf_score = 0.0
        for t in task_terms:
            if t in content_lower:
                df = term_doc_freq.get(t, 1)
                idf = math.log(n_files / max(df, 1))  # high when term is rare
                tf = min(content_lower.count(t), 5)    # cap TF at 5
                tfidf_score += tf * idf

        # Filename match (very strong signal)
        name_hits = sum(1 for t in task_terms if t in name_lower)

        # ENTITY DEFINITION — strongest structural signal
        # Bonus scales with codebase size: larger repos need stronger entity signal
        entity_match_count = 0
        for entity in entities:
            ent_lower = entity.name.lower()
            for t in task_terms:
                if t in ent_lower or ent_lower.startswith(t) or ent_lower.endswith(t):
                    entity_match_count += 1
                    break

        # Structural bonus
        path_parts_lower = {p.lower() for p in path.parts}
        core_file = bool(path_parts_lower & {"core", "domain", "models", "entities"})

        # Scale entity bonus by codebase size — in large repos, entity defs
        # must dominate keyword noise. Scale aggressively.
        entity_bonus_multiplier = max(50, n_files // 10)  # 50 for small, 300+ for large

        # Combine: TF-IDF + entity defs + structural signals
        relevance = (
            tfidf_score                                    # TF-IDF weighted keywords
            + name_hits * 15                               # filename match
            + entity_match_count * entity_bonus_multiplier # entity definition
            + (20 if core_file else 0)                     # core/domain directory
        )

        if relevance == 0:
            continue

        # Centrality: how many other types/files reference this file's types
        # Use type_graph for accurate C# centrality, fallback to import_counts
        centrality = 0
        for entity in entities:
            refs = type_graph.get_referenced_by(entity.name)
            centrality += len(refs)
        if centrality == 0:
            centrality = import_counts.get(name_lower, 0)

        # Size penalty (prefer focused files over huge ones)
        size_kb = max(len(content) / 1024, 0.1)

        voi = relevance * (1 + math.log1p(centrality)) / math.log1p(size_kb)

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

    # Auto-scale max_files based on codebase size
    if max_files <= 0:
        max_files = min(200, max(30, len(all_files) // 20))

    # INJECTION: For large codebases, use the type graph bidirectionally:
    # 1. Find types that REFERENCE top scored types (who uses AirlineGroup?)
    # 2. Find types REFERENCED BY top scored types (what does Film use?)
    # Inject these files into scored set with median VoI.
    if type_graph and len(all_files) > 100:
        scored_stems = {fs.path.stem.lower() for fs in scored[:max_files]}
        injected = []
        median_voi = scored[min(max_files // 2, len(scored) - 1)].voi if scored else 1.0

        for fs in scored[:30]:
            for entity in fs.entities:
                ent_lower = entity.name.lower()
                # Bidirectional: both incoming and outgoing references
                neighbors = set()
                # Outgoing: what this file's types reference
                neighbors |= type_graph.get_references_from_file(fs.path.stem)
                # Incoming: what references this file's types
                neighbors |= type_graph.get_referenced_by(ent_lower)

                for ref_name in neighbors:
                    if ref_name in scored_stems:
                        continue
                    ref_file = type_graph.get_type_file(ref_name)
                    if not ref_file or str(ref_file) not in file_contents:
                        continue
                    content, content_lower, ref_entities = file_contents[str(ref_file)]
                    ref_centrality = sum(len(type_graph.get_referenced_by(e.name)) for e in ref_entities)
                    injected_fs = FileScore(
                        path=ref_file, relevance=100, centrality=ref_centrality,
                        entity_count=len(ref_entities), voi=median_voi,
                        content=content[:4000], entities=ref_entities,
                    )
                    injected.append(injected_fs)
                    scored_stems.add(ref_file.stem.lower())

        if injected:
            scored.extend(injected)
            scored.sort(key=lambda x: -x.voi)

    return scored[:max_files]


# ── Task term extraction ──────────────────────────────────────────────────────

def extract_task_terms(task: str) -> set[str]:
    """Extract meaningful terms from task description."""
    stop = {
        "a", "an", "the", "and", "or", "for", "to", "of", "in",
        "is", "be", "add", "get", "set", "do", "make", "use",
        "with", "that", "this", "from", "as", "by", "on", "at",
        "it", "its", "can", "will", "should", "must", "need",
        "want", "when", "where", "how", "what", "which",
        "not", "but", "are", "has", "have", "had", "been",
        "any", "all", "each", "some", "new", "also",
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

    # Extract hyphenated compound terms (e.g., "cold-chain", "re-routing")
    compounds = re.findall(r"[A-Za-z]+-[A-Za-z]+", task)
    for c in compounds:
        terms.add(c.lower().replace("-", "_"))
        terms.add(c.lower().replace("-", ""))

    # Generate snake_case compounds from adjacent word pairs
    task_words = [w.lower() for w in re.findall(r"[A-Za-z]{3,}", task) if w.lower() not in stop]
    for i in range(len(task_words) - 1):
        w1, w2 = task_words[i], task_words[i+1]
        terms.add(f"{w1}_{w2}")
        # Also add singular forms (strip trailing 's' or 'es')
        w1s = w1.rstrip("s") if w1.endswith("s") and len(w1) > 4 else w1
        w2s = w2.rstrip("s") if w2.endswith("s") and len(w2) > 4 else w2
        if w1s != w1 or w2s != w2:
            terms.add(f"{w1s}_{w2s}")

    # Also add singular forms of individual terms
    for t in list(terms):
        if t.endswith("s") and len(t) > 4:
            terms.add(t[:-1])
        if t.endswith("es") and len(t) > 5:
            terms.add(t[:-2])

    return terms


# ── Verification question generation ─────────────────────────────────────────

@dataclass
class VerificationQuestion:
    """An auto-generated question with a known answer from the codebase."""
    question: str
    answer: str           # ground truth from code
    source: str           # file:line
    term: str             # which task term this verifies


def _match_term_to_entity(entity_name: str, task_terms: set[str]) -> str | None:
    """Match an entity to the best task term, with fuzzy/partial matching."""
    matches = _match_all_terms_to_entity(entity_name, "", task_terms)
    return matches[0] if matches else None


def _match_all_terms_to_entity(
    entity_name: str, file_content_lower: str, task_terms: set[str]
) -> list[str]:
    """Match an entity to ALL matching task terms, sorted by specificity (longest first).

    Also checks if the file content mentions the term near the entity definition,
    which helps catch cases like CustomerTier defining PREMIUM.
    """
    entity_lower = entity_name.lower()
    entity_parts = set(
        p.lower() for p in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)", entity_name)
        if len(p) >= 3
    )
    entity_snake = "_".join(sorted(entity_parts))

    # Two tiers: direct (entity name match) and indirect (content match)
    direct_matches = set()
    content_matches = set()

    for t in task_terms:
        # Direct containment
        if t in entity_lower or entity_lower in t:
            direct_matches.add(t)
            continue

        # Partial match: entity parts overlap with term parts
        t_parts = set(p for p in t.split("_") if len(p) >= 3)
        if entity_parts and t_parts and (entity_parts & t_parts):
            direct_matches.add(t)
            continue

        # Snake-case match
        if t in entity_snake or entity_snake in t:
            direct_matches.add(t)
            continue

        # Content match: term appears as a DEFINED VALUE in the file
        # (e.g., PREMIUM = "premium", or class attribute, or constant)
        # Only for single-word terms to avoid noise
        if file_content_lower and "_" not in t and len(t) >= 4:
            # Check if term appears as an uppercase constant or quoted value
            if (t.upper() in (file_content_lower.replace(t.lower(), t.upper()))
                    or f'"{t}"' in file_content_lower
                    or f"'{t}'" in file_content_lower
                    or f".{t}" in file_content_lower
                    or f"{t} =" in file_content_lower):
                content_matches.add(t)

    # Sort each tier by length descending, direct matches first
    direct = sorted(direct_matches, key=len, reverse=True)
    content = sorted(content_matches, key=len, reverse=True)
    return direct + content


def _trace_imports_for_terms(scored_files: list[FileScore], task_terms: set[str]) -> dict[str, list[str]]:
    """For each task term, find files where it's referenced via imports.

    If file A uses term T and imports from file B, then B is relevant to T.
    Returns: {term: [filenames that define entities related to term via imports]}
    """
    term_files: dict[str, list[str]] = {}

    for fs in scored_files:
        content_lower = fs.content.lower() if fs.content else ""
        file_stem = fs.path.stem.lower()

        for term in task_terms:
            # Check if this file mentions the term
            if term in content_lower or term.replace("_", "") in content_lower:
                if term not in term_files:
                    term_files[term] = []
                term_files[term].append(file_stem)

    return term_files


def _find_defining_file(term: str, scored_files: list[FileScore]) -> FileScore | None:
    """Find the file that most likely DEFINES a term.

    Checks for: term in filename, term as constant/value, term as class member.
    Returns the best-scoring file or None.
    """
    best_file = None
    best_score = 0

    for fs in scored_files:
        if not fs.entities:
            continue
        file_stem = fs.path.stem.lower()
        content = fs.content or ""
        content_lower = content.lower()

        score = 0
        # Also check space-separated variant for compound terms
        term_spaced = term.replace("_", " ")
        term_joined = term.replace("_", "")

        # Term in filename is strongest signal
        if term in file_stem or term_joined in file_stem:
            score += 10
        # Term appears as UPPER_CASE constant (e.g., PREMIUM = ...)
        if term.upper() in content:
            score += 8
        # Term appears as quoted value (e.g., "premium")
        if f'"{term}"' in content_lower or f"'{term}'" in content_lower:
            score += 6
        # Term appears as class/attribute definition
        if f"{term} =" in content_lower or f".{term}" in content_lower:
            score += 4
        # Raw occurrence count (check both underscore and space variants)
        count = content_lower.count(term)
        if "_" in term:
            count += content_lower.count(term_spaced)
            count += content_lower.count(term_joined)
        score += count

        if score > best_score:
            best_score = score
            best_file = fs

    return best_file if best_score > 0 else None


def generate_verification_questions(
    scored_files: list[FileScore],
    task_terms: set[str],
    max_questions: int = 40,
    type_graph: TypeGraph | None = None,
) -> list[VerificationQuestion]:
    """
    Generate objective questions from AST facts.
    Two strategies combined:
    1. Entity-driven: match entities to task terms by name
    2. Term-driven: for each task term, find where it's DEFINED and ask about that file
    """
    questions: list[VerificationQuestion] = []
    seen_sources: set[str] = set()  # track source (file:line) to avoid dupes
    covered_terms: set[str] = set()

    # Strategy 1: Entity-driven — match entities to task terms
    # Two passes: (A) exact name matches first, (B) then partial matches
    # Dynamic allocation: small codebases need more S1, large need more S3
    codebase_size = len(scored_files)
    if codebase_size < 50:
        strategy1_pct = 0.60   # small repo: keyword matching is sufficient
    elif codebase_size < 150:
        strategy1_pct = 0.45   # medium repo
    else:
        strategy1_pct = 0.35   # large repo: need room for type graph discovery
    strategy1_max = int(max_questions * strategy1_pct)

    # Pass A: EXACT matches — entity name IS the term (case-insensitive)
    # Sort: prefer Core/Domain directories and .cs/.py over .ts/.tsx
    def entity_file_priority(fs):
        parts_lower = {p.lower() for p in fs.path.parts}
        is_core = bool(parts_lower & {"core", "domain", "models", "entities"})
        is_backend = fs.path.suffix in (".cs", ".py")
        return (0 if is_core and is_backend else 1 if is_backend else 2 if is_core else 3, -fs.voi)

    sorted_for_exact = sorted(scored_files, key=entity_file_priority)

    for fs in sorted_for_exact:
        for entity in fs.entities:
            ent_lower = entity.name.lower()
            exact_term = None
            for t in task_terms:
                if ent_lower == t or ent_lower == t.replace("_", ""):
                    exact_term = t
                    break
            if not exact_term or exact_term in covered_terms:
                continue

            q = _make_question(entity, fs, exact_term)
            if q:
                questions.append(q)
                seen_sources.add(q.source)
                covered_terms.add(exact_term)

            if len(questions) >= strategy1_max:
                break
        if len(questions) >= strategy1_max:
            break

    # Pass B: partial matches — entity name CONTAINS a term
    if len(questions) < strategy1_max:
        for fs in scored_files:
            for entity in fs.entities:
                term = _match_term_to_entity(entity.name, task_terms)
                if not term or term in covered_terms:
                    continue

                q = _make_question(entity, fs, term)
                if q and q.source not in seen_sources:
                    questions.append(q)
                    seen_sources.add(q.source)
                    covered_terms.add(term)

                if len(questions) >= strategy1_max:
                    break
            if len(questions) >= strategy1_max:
                break

    # Strategy 2: Term-driven — for EVERY uncovered term, find the defining file
    # and generate a question pointing there. This catches trap terms like
    # "premium" which lives in customer_tier.py even though no entity is named Premium.
    #
    # Prioritize terms that resolve to NEW files (not yet covered by Strategy 1)
    # — these are the most valuable for trap detection.
    uncovered = task_terms - covered_terms
    covered_files = {q.source.split(":")[0] for q in questions}

    # Sort: filename-matching terms first (strongest trap signal), then new files
    def strategy2_priority(term):
        df = _find_defining_file(term, scored_files)
        if not df:
            return (3, 0, 0)  # no defining file -> lowest priority
        file_stem = df.path.stem.lower()
        filename_match = 0 if (term in file_stem or term.replace("_", "") in file_stem) else 1
        new_file = 0 if df.path.name not in covered_files else 1
        return (filename_match, new_file, -df.voi)

    # Dynamic S2 cap: small repos don't need S3 so S2 can use more
    strategy2_max = int(max_questions * (0.85 if codebase_size < 50 else 0.6))
    for term in sorted(uncovered, key=strategy2_priority):
        if len(questions) >= strategy2_max:
            break

        defining_file = _find_defining_file(term, scored_files)
        if not defining_file:
            continue

        # Pick the first entity in the defining file
        # Allow reuse of seen_sources — Strategy 2 generates a DIFFERENT question
        # for the same entity but with a different term
        for entity in defining_file.entities:
            q = _make_question(entity, defining_file, term)
            if q:
                questions.append(q)
                covered_terms.add(term)
                break

    # Strategy 3: TYPE GRAPH DISCOVERY — use the type reference graph to find
    # hidden domain concepts. For each entity in questions, find 1-hop neighbors
    # in the type graph. Discovers Slates (referenced by Film), Territory (same namespace).
    # Use passed type_graph or fall back to module cache
    tg = type_graph or _cached_type_graph
    if len(questions) < max_questions and tg:
        covered_entity_names = {q.term.lower() for q in questions}
        discovered: dict[str, Path] = {}

        # Collect seed type names from existing questions
        seed_types = set()
        for q in questions:
            for word in q.question.split("`"):
                w = word.strip()
                if len(w) > 2 and w[0:1].isupper() and w.isalnum():
                    seed_types.add(w.lower())
            seed_types.add(q.term.split("_")[0].lower())

        # Find 1-hop neighbors via type graph
        # Track DIRECT references (file A uses type B) vs namespace siblings
        direct_refs: set[str] = set()
        for seed in seed_types:
            locs = tg.types.get(seed, [])
            for loc in locs:
                file_refs = tg.file_references.get(loc.file.stem.lower(), set())
                direct_refs |= file_refs

            neighbors = tg.get_neighbors(seed, depth=1)
            for neighbor in neighbors:
                if neighbor in covered_entity_names or len(neighbor) < 4:
                    continue
                target_file = tg.get_type_file(neighbor)
                if target_file:
                    discovered[neighbor] = target_file

        # Sort: prioritize informative domain entities over infrastructure
        noise_suffixes = {"service", "repository", "factory", "handler", "validator",
                         "extension", "extensions", "helper", "configuration", "installer",
                         "controller", "middleware", "provider", "exception", "options",
                         "mongodocument", "elasticdocument", "response", "request", "command",
                         "query", "dto", "mapper", "converter", "base"}

        def discovery_priority(item):
            name, path = item
            # Penalize infrastructure/boilerplate types
            is_noise = any(name.endswith(s) for s in noise_suffixes)
            is_direct = name in direct_refs
            parts = {p.lower() for p in path.parts}
            is_core = bool(parts & {"core", "domain", "models"})
            is_backend = path.suffix in (".cs", ".py")
            return (
                0 if is_direct and is_core and not is_noise else
                1 if is_direct and not is_noise else
                2 if is_core and not is_noise else
                3 if not is_noise else 4,
                name
            )

        for ent_name, ent_path in sorted(discovered.items(), key=discovery_priority):
            if len(questions) >= max_questions:
                break
            try:
                content = ent_path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            entities = extract_entities(content, ent_path)
            target_entity = None
            for e in entities:
                if e.name.lower() == ent_name:
                    target_entity = e
                    break
            if not target_entity and entities:
                target_entity = entities[0]
            if target_entity:
                mini_fs = FileScore(
                    path=ent_path, relevance=0, centrality=0,
                    entity_count=len(entities), voi=0, content=content[:4000],
                    entities=entities
                )
                q = _make_question(target_entity, mini_fs, ent_name)
                if q:
                    questions.append(q)
                    covered_entity_names.add(ent_name)

    return questions


def _make_question(entity: CodeEntity, fs: FileScore, term: str) -> VerificationQuestion | None:
    """Create a verification question from an entity."""
    # Include parent dirs for nested codebases
    parts = fs.path.parts
    root_markers = {"src", "lib", "app", "pkg", "engine", "gateway", "server",
                    "modules", "packages", "addons", "apps"}
    source_path = fs.path.name
    for i, p in enumerate(parts):
        if p.lower() in root_markers:
            source_path = "/".join(parts[i:])
            break
    else:
        # No marker: use last 3 components (e.g. saleor/order/models.py)
        source_path = "/".join(parts[-min(3, len(parts)):])
    source = f"{source_path}:{entity.line}"

    if entity.kind == "class":
        return VerificationQuestion(
            question=f"What is `{entity.name}` and what does it extend/implement?",
            answer=f"`{entity.name}` is a class in {source}"
                   + (f", extends/implements: {entity.signature}" if entity.signature else ""),
            source=source,
            term=term,
        )
    elif entity.kind in ("function", "method"):
        return VerificationQuestion(
            question=f"What parameters does `{entity.name}()` accept?",
            answer=f"`{entity.name}` in {source} accepts: ({entity.signature or 'none'})",
            source=source,
            term=term,
        )
    elif entity.kind == "interface":
        return VerificationQuestion(
            question=f"What is the interface `{entity.name}` and what does it extend?",
            answer=f"`{entity.name}` is an interface in {source}"
                   + (f", extends: {entity.signature}" if entity.signature else ""),
            source=source,
            term=term,
        )
    elif entity.kind == "enum":
        return VerificationQuestion(
            question=f"What values does enum `{entity.name}` define?",
            answer=f"`{entity.name}` is an enum in {source}",
            source=source,
            term=term,
        )
    return None


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
