"""
type_graph.py -- Full type reference graph for codebases.

Builds a map of which types reference which other types, across:
- C# (namespace-aware: using + type references in code)
- TypeScript (import + type references)
- Python (import + type references)

This is the missing piece that allows epistemix to discover hidden
domain concepts like "Slates" that aren't mentioned in the task
but are architecturally required.
"""

import re
from pathlib import Path
from dataclasses import dataclass, field


@dataclass
class TypeInfo:
    """A named type (class/interface/enum/record) with its location."""
    name: str
    qualified_name: str  # namespace.TypeName
    file: Path
    line: int
    kind: str  # class, interface, enum, record
    namespace: str = ""


@dataclass
class TypeGraph:
    """Graph of type definitions and references."""
    # type_name_lower -> [TypeInfo] (multiple files can define same name)
    types: dict[str, list[TypeInfo]] = field(default_factory=dict)
    # file_stem_lower -> set of type_name_lower it references
    file_references: dict[str, set[str]] = field(default_factory=dict)
    # type_name_lower -> set of type_name_lower that reference it
    referenced_by: dict[str, set[str]] = field(default_factory=dict)
    # namespace -> set of type names in that namespace
    namespace_types: dict[str, set[str]] = field(default_factory=dict)

    def get_type_file(self, type_name: str) -> Path | None:
        """Get the file defining a type, preferring Core/Domain directories."""
        locs = self.types.get(type_name.lower(), [])
        if not locs:
            return None
        # Prefer Core/Domain backend files
        for ti in locs:
            parts = {p.lower() for p in ti.file.parts}
            if parts & {"core", "domain", "models"} and ti.file.suffix == ".cs":
                return ti.file
        return locs[0].file

    def get_references_from_file(self, file_stem: str) -> set[str]:
        """Get all types referenced by a file."""
        return self.file_references.get(file_stem.lower(), set())

    def get_referenced_by(self, type_name: str) -> set[str]:
        """Get all types that reference this type."""
        return self.referenced_by.get(type_name.lower(), set())

    def get_neighbors(self, type_name: str, depth: int = 1) -> set[str]:
        """Get types within N hops of a given type."""
        visited = set()
        current = {type_name.lower()}
        for _ in range(depth):
            next_level = set()
            for t in current:
                if t in visited:
                    continue
                visited.add(t)
                # Types in the same namespace
                for ns, ns_types in self.namespace_types.items():
                    if t in ns_types:
                        next_level |= ns_types
                # Types referenced by this type's file
                locs = self.types.get(t, [])
                for loc in locs:
                    refs = self.file_references.get(loc.file.stem.lower(), set())
                    next_level |= refs
                # Types that reference this type
                next_level |= self.referenced_by.get(t, set())
            current = next_level - visited
        return visited | current


SKIP_DIRS = {
    "node_modules", ".git", "bin", "obj", ".next", "dist", "build",
    "__pycache__", ".pytest_cache", "migrations", "wwwroot", "packages",
    ".vs", ".idea", "coverage", "TestResults", ".nuget", "artifacts",
    "tests", "test", "Tests", "Test", "__tests__", "spec",
}

# Patterns to extract type declarations
CS_TYPE_DECL = re.compile(
    r"(?:public|internal|private|protected)\s*"
    r"(?:abstract|sealed|static|partial|new)?\s*"
    r"(?:partial\s+)?"
    r"(class|interface|enum|record|struct)\s+"
    r"(\w+)"
)

CS_NAMESPACE = re.compile(r"namespace\s+([\w.]+)")

# Patterns to extract type REFERENCES (not just declarations)
# Matches: new TypeName, : TypeName, <TypeName>, TypeName.Method, (TypeName
CS_TYPE_REF = re.compile(r"(?:new\s+|:\s*|<|,\s*|\(\s*)([A-Z]\w{2,})(?:\s*[<>().,;\s{])")

TS_TYPE_DECL = re.compile(
    r"(?:export\s+)?(?:default\s+)?(?:abstract\s+)?(?:declare\s+)?"
    r"(class|interface|type|enum)\s+"
    r"(\w+)"
)

# Additional TS patterns for React/functional components
TS_COMPONENT_DECL = re.compile(
    r"(?:export\s+)?(?:const|function)\s+([A-Z]\w+)\s*[:=]"
)

TS_IMPORT = re.compile(r"""(?:import|from)\s*['"(]([^'"()]+)['")]""")

PY_TYPE_DECL = re.compile(r"class\s+(\w+)")


def build_type_graph(root: Path) -> TypeGraph:
    """Build a complete type reference graph for a codebase."""
    graph = TypeGraph()

    code_files = []
    for ext in (".cs", ".ts", ".tsx", ".py"):
        for f in root.rglob(f"*{ext}"):
            if not any(s in f.parts for s in SKIP_DIRS):
                code_files.append(f)

    # Pass 1: Collect all type declarations
    for f in code_files:
        try:
            content = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        if f.suffix == ".cs":
            _index_cs_types(f, content, graph)
        elif f.suffix in (".ts", ".tsx"):
            _index_ts_types(f, content, graph)
        elif f.suffix == ".py":
            _index_py_types(f, content, graph)

    # Pass 2: Collect type references
    all_type_names = set(graph.types.keys())

    for f in code_files:
        try:
            content = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        file_stem = f.stem.lower()
        refs = set()

        if f.suffix == ".cs":
            refs = _extract_cs_refs(content, all_type_names)
        elif f.suffix in (".ts", ".tsx"):
            refs = _extract_ts_refs(content, all_type_names)
        elif f.suffix == ".py":
            refs = _extract_py_refs(content, all_type_names)

        # Remove self-references
        self_types = {ti.name.lower() for tlist in graph.types.values()
                      for ti in tlist if ti.file == f}
        refs -= self_types

        graph.file_references[file_stem] = refs

        # Build reverse index
        for ref in refs:
            if ref not in graph.referenced_by:
                graph.referenced_by[ref] = set()
            graph.referenced_by[ref].add(file_stem)

    return graph


def _index_cs_types(f: Path, content: str, graph: TypeGraph):
    ns = ""
    ns_match = CS_NAMESPACE.search(content)
    if ns_match:
        ns = ns_match.group(1)

    for line_no, line in enumerate(content.splitlines(), 1):
        m = CS_TYPE_DECL.search(line)
        if m:
            kind = m.group(1)
            name = m.group(2)
            if name in ("if", "for", "while", "get", "set", "value"):
                continue

            ti = TypeInfo(
                name=name, qualified_name=f"{ns}.{name}" if ns else name,
                file=f, line=line_no, kind=kind, namespace=ns
            )

            name_lower = name.lower()
            if name_lower not in graph.types:
                graph.types[name_lower] = []
            graph.types[name_lower].append(ti)

            if ns:
                if ns not in graph.namespace_types:
                    graph.namespace_types[ns] = set()
                graph.namespace_types[ns].add(name_lower)


def _index_ts_types(f: Path, content: str, graph: TypeGraph):
    seen = set()
    for line_no, line in enumerate(content.splitlines(), 1):
        m = TS_TYPE_DECL.search(line)
        if m:
            kind = m.group(1)
            name = m.group(2)
        else:
            # Try component pattern (exported PascalCase const/function)
            m2 = TS_COMPONENT_DECL.search(line)
            if m2:
                kind = "class"  # treat components as classes for graph purposes
                name = m2.group(1)
            else:
                continue

        if name in seen or name in ("If", "For", "Use", "Get", "Set"):
            continue
        seen.add(name)

        ti = TypeInfo(name=name, qualified_name=name, file=f,
                     line=line_no, kind=kind)
        name_lower = name.lower()
        if name_lower not in graph.types:
            graph.types[name_lower] = []
        graph.types[name_lower].append(ti)


def _index_py_types(f: Path, content: str, graph: TypeGraph):
    for line_no, line in enumerate(content.splitlines(), 1):
        m = PY_TYPE_DECL.search(line)
        if m:
            name = m.group(1)
            ti = TypeInfo(name=name, qualified_name=name, file=f,
                         line=line_no, kind="class")
            name_lower = name.lower()
            if name_lower not in graph.types:
                graph.types[name_lower] = []
            graph.types[name_lower].append(ti)


def _extract_cs_refs(content: str, known_types: set[str]) -> set[str]:
    """Extract type references from C# code."""
    refs = set()
    for m in CS_TYPE_REF.finditer(content):
        name = m.group(1).lower()
        if name in known_types:
            refs.add(name)

    # Also extract from using directives — namespace parts can be types
    for m in re.finditer(r"using\s+([\w.]+);", content):
        for part in m.group(1).split("."):
            pl = part.lower()
            if pl in known_types:
                refs.add(pl)

    return refs


def _extract_ts_refs(content: str, known_types: set[str]) -> set[str]:
    refs = set()
    # Import references (named imports)
    for m in re.finditer(r"import\s*\{([^}]+)\}", content):
        for name in m.group(1).split(","):
            name = name.strip().split(" as ")[0].strip().lower()
            if name in known_types:
                refs.add(name)
    # Default imports
    for m in re.finditer(r"import\s+(\w+)\s+from", content):
        name = m.group(1).lower()
        if name in known_types:
            refs.add(name)
    # Type annotations, instantiation, generics, extends, implements
    for m in re.finditer(r"(?::\s*|new\s+|<|extends\s+|implements\s+|typeof\s+)(\w+)", content):
        name = m.group(1).lower()
        if name in known_types:
            refs.add(name)
    # JSX component usage: <ComponentName
    for m in re.finditer(r"<([A-Z]\w+)", content):
        name = m.group(1).lower()
        if name in known_types:
            refs.add(name)
    return refs


def _extract_py_refs(content: str, known_types: set[str]) -> set[str]:
    refs = set()
    for m in re.finditer(r"(?:from\s+\S+\s+import\s+|import\s+)(\w+)", content):
        name = m.group(1).lower()
        if name in known_types:
            refs.add(name)
    for m in re.finditer(r"(?::\s*|->|class\s+\w+\()\s*(\w+)", content):
        name = m.group(1).lower()
        if name in known_types:
            refs.add(name)
    return refs
