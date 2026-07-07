"""Tests for the syntax repair layer (deterministic stages + editing validator)."""

from __future__ import annotations

from ssis_migration.transform.llm.repair import (
    RepairResult,
    SyntaxFixer,
    ensure_compilable,
    extract_code,
    normalize_code,
    syntax_error,
)


class FakeClient:
    """Offline stand-in for CopilotClient: returns canned responses in order."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def simple_complete(self, system: str, user: str, model=None, max_tokens=None) -> str:
        self.calls.append((system, user))
        if not self._responses:
            raise RuntimeError("FakeClient exhausted")
        return self._responses.pop(0)


# ─── extract_code ─────────────────────────────────────────────────────────────

def test_extract_from_fence():
    text = "Here's the conversion:\n```python\nx = 1\n```\nHope this helps!"
    assert extract_code(text) == "x = 1"


def test_extract_largest_fence():
    text = "```python\nx = 1\n```\nusage:\n```python\ny = 1\nz = x + y\nprint(z)\n```"
    assert "z = x + y" in extract_code(text)


def test_extract_strips_leading_prose():
    text = "Sure! Below is the converted function.\n\ndef run():\n    return 1"
    out = extract_code(text)
    assert out.startswith("def run():")


def test_extract_keeps_comments_and_imports():
    text = "# setup\nimport os\nx = os.getcwd()"
    assert extract_code(text) == text


def test_extract_drops_dangling_fence_line():
    text = "```python\nx = 1"      # opening fence never closed (truncation)
    assert extract_code(text) == "x = 1"


# ─── normalize_code ───────────────────────────────────────────────────────────

def test_normalize_smart_quotes_and_dashes():
    code = "x = “hello” + ‘world’  # em—dash"
    out = normalize_code(code)
    assert '"hello"' in out and "'world'" in out and "—" not in out


def test_normalize_tabs_and_uniform_indent():
    code = "    def f():\n\t    return 1"
    out = normalize_code(code)
    assert syntax_error(out) is None


def test_normalize_crlf():
    assert normalize_code("a = 1\r\nb = 2\r\n") == "a = 1\nb = 2\n"


# ─── ensure_compilable ────────────────────────────────────────────────────────

def test_valid_code_passes_untouched():
    res = ensure_compilable("x = 1\n")
    assert res.ok and res.stages == []


def test_deterministic_repair_no_llm_needed():
    broken_wrapper = "```python\nx = “1”\n```"
    res = ensure_compilable(broken_wrapper)
    assert res.ok
    assert res.stages == ["deterministic"]
    assert res.code == 'x = "1"\n'


def test_llm_fix_path():
    fixer = SyntaxFixer(FakeClient(["x = (1 + 2)\n"]), spark_version="3.3")
    res = ensure_compilable("x = (1 + 2\n", fixer=fixer)
    assert res.ok
    assert res.stages == ["llm_fix_1"]
    assert res.code == "x = (1 + 2)\n"


def test_llm_fix_second_attempt():
    fixer = SyntaxFixer(
        FakeClient(["y = [1, 2\n", "y = [1, 2]\n"]), spark_version="3.3",
    )
    res = ensure_compilable("y = [1, 2\n", fixer=fixer)
    assert res.ok
    assert res.stages == ["llm_fix_2"]


def test_unfixable_returns_not_ok_with_error():
    fixer = SyntaxFixer(FakeClient(["still ( broken", "still ( broken"]), spark_version="3.3")
    res = ensure_compilable("def f(:\n    pass", fixer=fixer, max_llm_fixes=2)
    assert res.ok is False
    assert res.error


def test_no_fixer_deterministic_only():
    res = ensure_compilable("def f(:\n    pass", fixer=None)
    assert res.ok is False


def test_fixer_receives_error_context():
    client = FakeClient(["x = 1\n"])
    fixer = SyntaxFixer(client, spark_version="2.4.8")
    ensure_compilable("x = (\n", fixer=fixer)
    system, user = client.calls[0]
    assert "TARGET PYTHON: 3.7" in system          # spark 2.4 → py3.7 constraints
    assert "line" in user.lower()
    assert "x = (" in user


def test_empty_input():
    res = ensure_compilable("")
    assert res.ok is False
