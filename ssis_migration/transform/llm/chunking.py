"""
Semantic chunking + agent memory for context-aware code generation.

Why chunk?  One-shot generation of a whole Script Task / SQL script has two
failure modes: the completion gets truncated (broken syntax), and late parts of
the output lose coherence with early parts. Splitting the SOURCE into
semantically meaningful units — SQL batches/statements, .NET methods — keeps
each completion small enough to finish, and an explicit AgentMemory carries the
context forward so chunk N is generated *aware* of what chunks 1..N-1 defined.

Why lexical retrieval (no embeddings)?  What later chunks need from earlier
ones is above all *identifier consistency* — reuse the same function/variable
names, don't redefine, don't drift. Identifier-overlap scoring retrieves
exactly that signal, is fully deterministic (reproducible runs, testable), and
adds zero dependencies. Facts and defined symbols are always injected; only the
per-chunk summaries compete for the retrieval budget.

Chunk kinds
───────────
  sql_batch      a GO-delimited T-SQL batch (natural semantic unit)
  sql_group      grouped top-level statements from an oversized batch
  dotnet_prologue usings / class header / fields of a Script Task
  dotnet_method  one C# or VB.NET method
  block          fallback: the whole body as a single chunk
"""

from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Bodies smaller than this are generated in one shot (fast path).
DEFAULT_CHUNK_THRESHOLD_LINES = 60
DEFAULT_CHUNK_THRESHOLD_CHARS = 3500
# Target upper bound for a single chunk after splitting.
DEFAULT_MAX_CHUNK_LINES = 80


@dataclass
class CodeChunk:
    index: int              # 1-based position in generation order
    total: int              # total chunks for the item
    kind: str               # sql_batch | sql_group | dotnet_prologue | dotnet_method | block
    title: str              # human-readable unit name ("batch 2: MERGE dbo.OrderFact")
    text: str               # the source text of this chunk


def should_chunk(
    text: str,
    threshold_lines: int = DEFAULT_CHUNK_THRESHOLD_LINES,
    threshold_chars: int = DEFAULT_CHUNK_THRESHOLD_CHARS,
) -> bool:
    return len(text.splitlines()) > threshold_lines or len(text) > threshold_chars


# ─── SQL chunking ─────────────────────────────────────────────────────────────

_GO_RE = re.compile(r"^\s*GO\s*(?:--.*)?$", re.IGNORECASE | re.MULTILINE)
_SQL_TITLE_RE = re.compile(
    r"\b(MERGE|INSERT\s+INTO|UPDATE|DELETE\s+FROM|SELECT|CREATE\s+\w+|"
    r"TRUNCATE\s+TABLE|EXEC(?:UTE)?|DECLARE|WHILE|IF|BEGIN\s+TRY)\b[ \t]*([\w\.\[\]#@]*)",
    re.IGNORECASE,
)


def _sql_title(sql: str) -> str:
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        m = _SQL_TITLE_RE.search(stripped)
        if m:
            verb = re.sub(r"\s+", " ", m.group(1).upper())
            target = m.group(2) or ""
            return f"{verb} {target}".strip()
        return stripped[:48]
    return "sql"


def _split_top_level_statements(sql: str) -> list[str]:
    """Split on ';' at depth 0, respecting strings, [identifiers] and comments."""
    statements: list[str] = []
    buf: list[str] = []
    depth = 0
    i, n = 0, len(sql)
    in_str = in_bracket = in_line_comment = in_block_comment = False
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if in_line_comment:
            buf.append(ch)
            if ch == "\n":
                in_line_comment = False
        elif in_block_comment:
            buf.append(ch)
            if ch == "*" and nxt == "/":
                buf.append(nxt); i += 1
                in_block_comment = False
        elif in_str:
            buf.append(ch)
            if ch == "'":
                if nxt == "'":          # escaped quote
                    buf.append(nxt); i += 1
                else:
                    in_str = False
        elif in_bracket:
            buf.append(ch)
            if ch == "]":
                in_bracket = False
        elif ch == "-" and nxt == "-":
            buf.append(ch); in_line_comment = True
        elif ch == "/" and nxt == "*":
            buf.append(ch); in_block_comment = True
        elif ch == "'":
            buf.append(ch); in_str = True
        elif ch == "[":
            buf.append(ch); in_bracket = True
        elif ch in "(":
            depth += 1; buf.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1); buf.append(ch)
        elif ch == ";" and depth == 0:
            buf.append(ch)
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def _group_statements(statements: list[str], max_lines: int) -> list[str]:
    """Greedily pack consecutive statements into chunks of ≤ max_lines."""
    groups: list[str] = []
    buf: list[str] = []
    buf_lines = 0
    for stmt in statements:
        stmt_lines = len(stmt.splitlines())
        if buf and buf_lines + stmt_lines > max_lines:
            groups.append("\n".join(buf))
            buf, buf_lines = [], 0
        buf.append(stmt)
        buf_lines += stmt_lines
    if buf:
        groups.append("\n".join(buf))
    return groups


def chunk_sql(sql: str, max_chunk_lines: int = DEFAULT_MAX_CHUNK_LINES) -> list[CodeChunk]:
    """GO batches are hard boundaries; oversized batches split at top-level ';'."""
    pieces: list[tuple[str, str]] = []      # (kind, text)
    batches = [b.strip() for b in _GO_RE.split(sql) if b and b.strip()]
    for batch in batches:
        if len(batch.splitlines()) <= max_chunk_lines:
            pieces.append(("sql_batch", batch))
        else:
            stmts = _split_top_level_statements(batch)
            if len(stmts) <= 1:
                pieces.append(("sql_batch", batch))
            else:
                for group in _group_statements(stmts, max_chunk_lines):
                    pieces.append(("sql_group", group))

    total = len(pieces)
    return [
        CodeChunk(index=i + 1, total=total, kind=kind,
                  title=f"{'batch' if kind == 'sql_batch' else 'stmts'} {i + 1}: {_sql_title(text)}",
                  text=text)
        for i, (kind, text) in enumerate(pieces)
    ]


# ─── .NET (C# / VB.NET) chunking ─────────────────────────────────────────────

_CSHARP_METHOD_RE = re.compile(
    r"^[ \t]*(?:\[[^\]]+\][ \t]*\n[ \t]*)*"                       # attributes
    r"(?:public|private|protected|internal)[ \t]"                 # access modifier
    r"(?:static[ \t]|override[ \t]|async[ \t]|virtual[ \t]|sealed[ \t])*"
    r"[\w<>\[\],. ]+?[ \t](\w+)[ \t]*\([^;]*?\)[ \t]*(?:\{|\n)",  # name(args) {
    re.MULTILINE,
)
_VB_METHOD_RE = re.compile(
    r"^[ \t]*(?:Public|Private|Protected|Friend)?[ \t]*(?:Shared[ \t]+)?"
    r"(?:Sub|Function)[ \t]+(\w+)",
    re.MULTILINE | re.IGNORECASE,
)


def chunk_dotnet(code: str, language: str = "csharp") -> list[CodeChunk]:
    """
    Split a Script Task body at method boundaries. Everything before the first
    method (usings, namespace/class header, fields) becomes a prologue chunk —
    it defines the state the methods share, so it is generated first and its
    symbols land in memory before any method is converted.
    """
    pattern = _VB_METHOD_RE if language.lower().startswith("vb") else _CSHARP_METHOD_RE
    matches = list(pattern.finditer(code))
    if len(matches) <= 1:
        return [CodeChunk(index=1, total=1, kind="block", title="script body", text=code)]

    pieces: list[tuple[str, str, str]] = []   # (kind, title, text)
    first_start = matches[0].start()
    prologue = code[:first_start].strip()
    if prologue:
        pieces.append(("dotnet_prologue", "prologue (usings/fields)", prologue))

    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(code)
        body = code[m.start():end].strip()
        pieces.append(("dotnet_method", f"method {m.group(1)}", body))

    total = len(pieces)
    return [
        CodeChunk(index=i + 1, total=total, kind=kind, title=title, text=text)
        for i, (kind, title, text) in enumerate(pieces)
    ]


def chunk_source(text: str, language: str) -> list[CodeChunk]:
    """Route to the right chunker; single block when small."""
    if not should_chunk(text):
        return [CodeChunk(index=1, total=1, kind="block", title="body", text=text)]
    if language in ("tsql", "sql"):
        return chunk_sql(text)
    return chunk_dotnet(text, language)


# ─── Agent memory ─────────────────────────────────────────────────────────────

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_STOPWORDS = frozenset({
    "the", "and", "for", "not", "from", "import", "def", "return", "with",
    "self", "none", "true", "false", "spark", "params", "connections", "if",
    "else", "in", "is", "as", "or", "select", "where", "into", "values",
})


def _tokens(text: str) -> set[str]:
    return {
        t.lower() for t in _IDENT_RE.findall(text)
        if len(t) > 2 and t.lower() not in _STOPWORDS
    }


@dataclass
class _ChunkNote:
    title: str
    symbols: list[str]
    tokens: set[str] = field(default_factory=set)


class AgentMemory:
    """
    Per-package working memory shared by all generation agents.

    Contents:
      facts           package-level truths (params, connections, spark target) —
                      ALWAYS injected.
      symbols         names defined by previously generated code (AST-extracted)
                      — ALWAYS injected, so later chunks reuse instead of
                      redefining or misspelling.
      chunk notes     one summary per generated chunk — retrieved by lexical
                      overlap with the chunk being generated (top-K in budget).
      issues          reviewer/validator feedback seen this package — recent
                      entries injected so regenerations don't repeat mistakes.
    """

    def __init__(self, facts: dict[str, str] | None = None) -> None:
        self.facts: dict[str, str] = dict(facts or {})
        self._symbols: dict[str, None] = {}          # ordered set
        self._notes: list[_ChunkNote] = []
        self._issues: dict[str, None] = {}           # ordered set (dedup)

    # -- writes -----------------------------------------------------------------

    def add_fact(self, key: str, value: str) -> None:
        self.facts[key] = value

    def record_code(self, title: str, code: str) -> None:
        symbols = _extract_symbols(code)
        for s in symbols:
            self._symbols.setdefault(s, None)
        self._notes.append(_ChunkNote(title=title, symbols=symbols, tokens=_tokens(code)))

    def record_issues(self, issues: list[str]) -> None:
        for issue in issues:
            key = issue.strip()
            if key:
                self._issues.setdefault(key, None)

    # -- reads ------------------------------------------------------------------

    @property
    def symbols(self) -> list[str]:
        return list(self._symbols)

    @property
    def issues(self) -> list[str]:
        return list(self._issues)

    def relevant_notes(self, query_text: str, k: int = 4) -> list[_ChunkNote]:
        if not self._notes:
            return []
        query = _tokens(query_text)
        scored = sorted(
            self._notes,
            key=lambda n: len(query & n.tokens),
            reverse=True,
        )
        return scored[:k]

    def render(self, query_text: str = "", budget_chars: int = 1800) -> str:
        """Render the memory block injected into generation prompts."""
        sections: list[str] = []
        if self.facts:
            facts = "; ".join(f"{k}={v}" for k, v in self.facts.items())
            sections.append(f"Package facts: {facts}")
        if self._symbols:
            names = ", ".join(list(self._symbols)[:60])
            sections.append(
                "Symbols already defined by previous chunks (REUSE these exact "
                f"names, do NOT redefine them): {names}"
            )
        notes = self.relevant_notes(query_text)
        if notes:
            lines = [
                f"  - {n.title}: defines {', '.join(n.symbols[:8]) or '(no symbols)'}"
                for n in notes
            ]
            sections.append("Previously converted chunks (context):\n" + "\n".join(lines))
        if self._issues:
            recent = list(self._issues)[-5:]
            sections.append(
                "Known pitfalls from earlier reviews (do NOT repeat):\n"
                + "\n".join(f"  - {i}" for i in recent)
            )
        block = "\n".join(sections)
        if len(block) > budget_chars:
            block = block[:budget_chars] + "\n  … (memory truncated)"
        return block


def _extract_symbols(code: str) -> list[str]:
    """Names a generated Python snippet defines (functions, classes, assignments)."""
    symbols: dict[str, None] = {}
    try:
        tree = ast.parse(code)
    except SyntaxError:
        for m in re.finditer(r"^(?:def|class)\s+(\w+)|^(\w+)\s*=", code, re.MULTILINE):
            symbols.setdefault(m.group(1) or m.group(2), None)
        return list(symbols)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.setdefault(node.name, None)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    symbols.setdefault(target.id, None)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            symbols.setdefault(node.target.id, None)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                symbols.setdefault(alias.asname or alias.name.split(".")[0], None)
    return list(symbols)
