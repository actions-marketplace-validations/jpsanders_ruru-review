"""
Tamper audit: did the change weaken or bypass the tests? Static and fast; nothing runs.

It compares the base and head versions of test files, ``conftest.py`` files, test and coverage configuration, CI
workflows and source pragmas.
"""

import ast
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ruru_review import gitio, pyast

HIGH, MEDIUM = "high", "medium"
STRONG_METHODS = {
    "assertEqual",
    "assertEquals",
    "assertListEqual",
    "assertDictEqual",
    "assertTupleEqual",
    "assertSetEqual",
    "assertSequenceEqual",
    "assertMultiLineEqual",
    "assertAlmostEqual",
    "assertCountEqual",
    "assertRaises",
    "assertRaisesRegex",
    "assertIs",
    "assertIsNone",
    "assertGreater",
    "assertLess",
    "assertGreaterEqual",
    "assertLessEqual",
    "assertRegex",
    "assertNotEqual",
}
WEAK_METHODS = {"assertTrue", "assertFalse", "assertIsNotNone", "assertIn", "assertIsInstance"}
SKIP_NAMES = {"skip", "skipif", "xfail", "importorskip", "skipIf", "skipUnless", "expectedFailure"}
HOOKS = {
    "pytest_collection_modifyitems",
    "pytest_ignore_collect",
    "pytest_collect_file",
    "pytest_collection",
    "pytest_deselected",
    "pytest_runtest_setup",
    "pytest_runtest_call",
    "pytest_runtest_makereport",
    "pytest_pycollect_makeitem",
    "pytest_configure",
    "pytest_sessionfinish",
    "pytest_report_teststatus",
}
CONFIG_FILES = {"pyproject.toml", "setup.cfg", "pytest.ini", "tox.ini", ".coveragerc"}
_ADDOPTS = re.compile(r"(^|\s)(-k\b|--deselect\b|--ignore\b|--ignore-glob\b|-p\s*no:|--co\b|--lf\b)")
_FAIL_UNDER = re.compile(r"fail[_-]under\s*[=:]\s*([0-9.]+)")


@dataclass
class Finding:
    """One way the change weakened the tests."""

    rule: str
    severity: str
    file: str
    line: int
    side: str
    message: str
    before: str = ""
    after: str = ""


def _calls(node: ast.AST) -> list[ast.Call]:
    return [n for n in ast.walk(node) if isinstance(n, ast.Call)]


def _strength(node: ast.AST) -> tuple[int, int, list[str]]:
    """
    Count strong and weak assertions in a test.

    :param node: A test function
    :returns: (strong, weak, weak assertion texts)
    """
    strong = weak = 0
    weak_text: list[str] = []
    for n in ast.walk(node):
        if isinstance(n, ast.Assert):
            t = n.test
            if isinstance(t, ast.Constant):
                weak += 1
                weak_text.append(ast.unparse(n))
            elif (
                isinstance(t, ast.Compare)
                and any(isinstance(o, (ast.Eq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.NotEq, ast.Is)) for o in t.ops)
                and not (
                    isinstance(t.ops[0], ast.IsNot)
                    and isinstance(t.comparators[0], ast.Constant)
                    and t.comparators[0].value is None
                )
            ):
                strong += 1
            else:
                weak += 1
                weak_text.append(ast.unparse(n))
        elif isinstance(n, ast.Call):
            name = pyast.dotted(n.func).rsplit(".", 1)[-1]
            if name in STRONG_METHODS:
                strong += 1
            elif name in WEAK_METHODS:
                weak += 1
                weak_text.append(ast.unparse(n))
            elif name == "raises" and n.args:
                if pyast.dotted(n.args[0]) in {"Exception", "BaseException"}:
                    weak += 1
                    weak_text.append(ast.unparse(n))
                else:
                    strong += 1
    return strong, weak, weak_text


def _assert_count(node: ast.AST) -> int:
    s, w, _ = _strength(node)
    return s + w


def _skips(node: ast.AST) -> list[str]:
    """
    Names of skip and xfail markers or calls in a node, each counted once.

    :param node: Any node
    :returns: Such as ``pytest.mark.skip``
    """
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute):
            name = pyast.dotted(n)
            if n.attr in SKIP_NAMES and name.split(".", 1)[0] in {"pytest", "unittest", "mark"}:
                out.append(name)
        elif isinstance(n, ast.Name) and n.id in SKIP_NAMES:
            out.append(n.id)
    return out


def _approx(node: ast.AST) -> list[float]:
    vals = []
    for c in _calls(node):
        if pyast.dotted(c.func).endswith("approx"):
            for kw in c.keywords:
                if (
                    kw.arg in {"rel", "abs"}
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, (int, float))
                ):
                    vals.append(float(kw.value.value))
    return vals


def _swallowed(node: ast.AST) -> tuple[int, int]:
    """
    Count ``try`` blocks that hold assertions, and handlers whose body is only ``pass``.

    :param node: A test function
    :returns: (tries around assertions, bare pass handlers)
    """
    wrapped = passes = 0
    for n in ast.walk(node):
        if isinstance(n, ast.Try):
            if any(isinstance(x, ast.Assert) for s in n.body for x in ast.walk(s)) and n.handlers:
                wrapped += 1
            passes += sum(1 for h in n.handlers if all(isinstance(s, ast.Pass) for s in h.body))
    return wrapped, passes


def _parametrize_cases(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    total = 0
    for d in node.decorator_list:
        if isinstance(d, ast.Call) and pyast.dotted(d.func).endswith("parametrize") and len(d.args) >= 2:
            if isinstance(d.args[1], (ast.List, ast.Tuple)):
                total += len(d.args[1].elts)
    return total


def _module_skips(tree: ast.Module | None) -> int:
    if tree is None:
        return 0
    n = 0
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        n += len(_skips(stmt))
    return n


def audit_test_file(path: str, old_path: str | None, before: str | None, after: str | None) -> list[Finding]:
    """
    Compare one test module's base and head versions.

    :param path: Head path, or base path when deleted
    :param old_path: Base path
    :param before: Base text
    :param after: Head text
    :returns: Findings
    """
    out: list[Finding] = []
    b_tree, a_tree = pyast.parse(before), pyast.parse(after)
    b_fns, a_fns = pyast.test_functions(b_tree), pyast.test_functions(a_tree)
    for name, fn in b_fns.items():
        if name not in a_fns:
            out.append(Finding("test-removed", HIGH, old_path or path, fn.lineno, "LEFT", f"test {name} was removed"))
    for name, fn in a_fns.items():
        old = b_fns.get(name)
        if old is None:
            if _skips(fn):
                out.append(
                    Finding("skip-added", HIGH, path, fn.lineno, "RIGHT", f"new test {name} is skipped or xfailed")
                )
            if fn.body and not _assert_count(fn) and not _calls(fn):
                out.append(Finding("empty-test", HIGH, path, fn.lineno, "RIGHT", f"new test {name} asserts nothing"))
            continue
        if pyast.dump(old) == pyast.dump(fn):
            continue
        bs, bw, _ = _strength(old)
        as_, aw, aw_text = _strength(fn)
        if as_ + aw < bs + bw:
            out.append(
                Finding(
                    "assertion-removed",
                    HIGH,
                    path,
                    fn.lineno,
                    "RIGHT",
                    f"{name} has {bs + bw - as_ - aw} fewer assertion(s) than before",
                )
            )
        elif as_ < bs:
            out.append(
                Finding(
                    "assertion-loosened",
                    HIGH,
                    path,
                    fn.lineno,
                    "RIGHT",
                    f"{name} traded exact assertions for weaker ones",
                    "",
                    "; ".join(aw_text[-3:]),
                )
            )
        if any("assert True" == t for t in aw_text) and not any("assert True" == t for t in _strength(old)[2]):
            out.append(Finding("assert-true", HIGH, path, fn.lineno, "RIGHT", f"{name} now asserts True"))
        if len(_skips(fn)) > len(_skips(old)):
            out.append(Finding("skip-added", HIGH, path, fn.lineno, "RIGHT", f"{name} gained a skip or xfail"))
        bw_try, bw_pass = _swallowed(old)
        aw_try, aw_pass = _swallowed(fn)
        if aw_try > bw_try:
            out.append(
                Finding("assertion-in-try", HIGH, path, fn.lineno, "RIGHT", f"{name} wraps assertions in try/except")
            )
        if aw_pass > bw_pass:
            out.append(
                Finding("except-pass", HIGH, path, fn.lineno, "RIGHT", f"{name} swallows an exception with pass")
            )
        b_cases, a_cases = _parametrize_cases(old), _parametrize_cases(fn)
        if a_cases < b_cases:
            out.append(
                Finding(
                    "cases-removed",
                    HIGH,
                    path,
                    fn.lineno,
                    "RIGHT",
                    f"{name} lost {b_cases - a_cases} parametrize case(s)",
                )
            )
        b_ap, a_ap = _approx(old), _approx(fn)
        if b_ap and a_ap and max(a_ap) > max(b_ap):
            out.append(
                Finding(
                    "approx-widened",
                    HIGH,
                    path,
                    fn.lineno,
                    "RIGHT",
                    f"{name} widened a pytest.approx tolerance",
                    str(max(b_ap)),
                    str(max(a_ap)),
                )
            )
    if _module_skips(a_tree) > _module_skips(b_tree):
        out.append(
            Finding("skip-added", HIGH, path, 1, "RIGHT", "the module gained a skip (pytestmark or importorskip)")
        )
    return out


def audit_conftest(path: str, before: str | None, after: str | None) -> list[Finding]:
    """
    Flag new collection or reporting hooks in a ``conftest.py``.

    :param path: Path
    :param before: Base text
    :param after: Head text
    :returns: Findings
    """

    def hooks(text: str | None) -> dict[str, int]:
        tree = pyast.parse(text)
        if tree is None:
            return {}
        return {n.name: n.lineno for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in HOOKS}

    b, a = hooks(before), hooks(after)
    out = [
        Finding(
            "harness-hook", HIGH, path, line, "RIGHT", f"conftest gained the hook {name}, which can change what runs"
        )
        for name, line in a.items()
        if name not in b
    ]
    b_tree, a_tree = pyast.parse(before), pyast.parse(after)
    if a_tree is not None and _module_skips(a_tree) > _module_skips(b_tree):
        out.append(Finding("skip-added", HIGH, path, 1, "RIGHT", "conftest gained a skip"))
    return out


def _added_lines(text: str | None, added: set[int]) -> list[tuple[int, str]]:
    if text is None:
        return []
    lines = text.splitlines()
    return [(n, lines[n - 1]) for n in sorted(added) if 0 < n <= len(lines)]


def audit_config(path: str, before: str | None, after: str | None, added: set[int]) -> list[Finding]:
    """
    Flag test and coverage configuration that lets less run or count.

    :param path: Path
    :param before: Base text
    :param after: Head text
    :param added: Added line numbers
    :returns: Findings
    """
    out = []
    for n, line in _added_lines(after, added):
        if "addopts" in line or line.strip().startswith(("-", '"-', "'-")):
            if _ADDOPTS.search(line):
                out.append(
                    Finding(
                        "addopts-deselect",
                        HIGH,
                        path,
                        n,
                        "RIGHT",
                        "test options now deselect or skip tests",
                        "",
                        line.strip(),
                    )
                )
        if re.search(r"^\s*(omit|exclude_lines|exclude_also)\b", line) or (
            line.strip().startswith(('"', "'")) and "cover" in path
        ):
            out.append(
                Finding("coverage-omit", HIGH, path, n, "RIGHT", "coverage now leaves code out", "", line.strip())
            )
    b = [float(x) for x in _FAIL_UNDER.findall(before or "")]
    a = [float(x) for x in _FAIL_UNDER.findall(after or "")]
    if b and (not a or min(a) < min(b)):
        out.append(
            Finding(
                "coverage-lowered",
                HIGH,
                path,
                1,
                "RIGHT",
                "the coverage floor was lowered or removed",
                str(min(b)),
                str(min(a)) if a else "removed",
            )
        )
    return out


def audit_workflow(path: str, after: str | None, added: set[int]) -> list[Finding]:
    """
    Flag CI steps that stop failing.

    :param path: Path
    :param after: Head text
    :param added: Added line numbers
    :returns: Findings
    """
    out = []
    for n, line in _added_lines(after, added):
        if re.search(r"\|\|\s*true\b|continue-on-error:\s*true", line):
            out.append(
                Finding(
                    "ci-ignores-failure", HIGH, path, n, "RIGHT", "a CI step no longer fails the job", "", line.strip()
                )
            )
    return out


def audit_source_pragmas(path: str, after: str | None, added: set[int]) -> list[Finding]:
    """
    Flag new ``# pragma: no cover`` in source.

    :param path: Path
    :param after: Head text
    :param added: Added line numbers
    :returns: Findings
    """
    return [
        Finding("no-cover-added", MEDIUM, path, n, "RIGHT", "source gained # pragma: no cover", "", line.strip())
        for n, line in _added_lines(after, added)
        if re.search(r"#\s*pragma:\s*no\s*cover", line)
    ]


def audit(
    repo: Path, base: str, head: str | None, files: list[gitio.FileChange], test_dirs: list[str]
) -> list[Finding]:
    """
    Run every tamper rule over a change.

    :param repo: Repository root
    :param base: Merge base
    :param head: Head commit or ``None``
    :param files: Changed files
    :param test_dirs: Test folder names
    :returns: Findings, high severity first
    """
    out: list[Finding] = []
    for f in files:
        before = gitio.read_at(repo, base, f.old_path) if f.old_path else None
        after = gitio.read_at(repo, head, f.new_path) if f.new_path else None
        name = PurePosixPath(f.path).name
        if pyast.is_test_file(f.path, test_dirs) or (f.old_path and pyast.is_test_file(f.old_path, test_dirs)):
            out += audit_test_file(f.path, f.old_path, before, after)
        elif name == "conftest.py":
            out += audit_conftest(f.path, before, after)
        elif name in CONFIG_FILES:
            out += audit_config(f.path, before, after, f.added)
        elif f.path.startswith(".github/workflows/") or name in {".gitlab-ci.yml", "noxfile.py", "Makefile"}:
            out += audit_workflow(f.path, after, f.added)
        elif name in {"sitecustomize.py", "usercustomize.py"} and before is None and after is not None:
            out.append(
                Finding("startup-hook", HIGH, f.path, 1, "RIGHT", f"new {name} runs code at every interpreter start")
            )
        elif f.path.endswith(".py") and after is not None:
            out += audit_source_pragmas(f.path, after, f.added)
    out.sort(key=lambda x: (x.severity != HIGH, x.file, x.line))
    return out
