"""
Run the project's tests: an import check, a baseline, coverage based test selection, then each mutant.

Mutants run in git worktrees at the head, one per worker, through the project's own interpreter. A mutant that
outlives its timeout has its whole process group killed and counts as killed.
"""

import os
import queue
import shlex
import signal
import sqlite3
import subprocess  # nosec B404: runs the project's own test command, as an argument list
import tempfile
import time
import xml.etree.ElementTree as ET  # nosec B405: parses JUnit XML pytest just wrote
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from ruru_review.mutants import Mutant

KILLED, SURVIVED, TIMEOUT, NOT_COVERED, SKIPPED = "killed", "survived", "timeout", "not_covered", "skipped"


class EnvironmentProblem(RuntimeError):
    """The project cannot be tested as it stands: exit code 2."""


@dataclass
class Proc:
    """What a test run did."""

    code: int
    seconds: float
    timed_out: bool
    output: str


@dataclass
class Project:
    """How to run the checked project's tests."""

    root: Path
    python: str
    command: list[str]
    src_root: str

    def env(self, tree: Path) -> dict[str, str]:
        """
        Build the environment for a run in ``tree``: its source root first on ``PYTHONPATH``.

        :param tree: A worktree
        :returns: Environment variables
        """
        env = dict(os.environ)
        first = str(tree / self.src_root) if self.src_root != "." else str(tree)
        rest = env.get("PYTHONPATH")
        env["PYTHONPATH"] = first + (os.pathsep + rest if rest else "")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.pop("PYTEST_ADDOPTS", None)
        env.pop("COVERAGE_PROCESS_START", None)
        return env


def find_python(root: Path, given: str | None) -> str:
    """
    Pick the project's interpreter: the one given, the project's ``.venv``, the active virtualenv, else python3.

    :param root: Project root
    :param given: ``--python``
    :returns: Interpreter path or name
    """
    if given:
        return str(Path(given).absolute()) if os.sep in given else given
    for cand in (root / ".venv" / "bin" / "python", root / "venv" / "bin" / "python"):
        if cand.exists():
            return str(cand)
    venv = os.environ.get("VIRTUAL_ENV")
    if venv and (Path(venv) / "bin" / "python").exists():
        return str(Path(venv) / "bin" / "python")
    return "python3"


def find_src_root(root: Path, given: str | None) -> str:
    """
    Pick the folder packages are imported from: ``src`` when it exists, else the root.

    :param root: Project root
    :param given: ``src`` from configuration
    :returns: A path relative to the root
    """
    if given:
        return given
    return "src" if (root / "src").is_dir() else "."


def make_project(root: Path, python: str | None, test_command: str | None, src: str | None) -> Project:
    """
    Describe how to test the project.

    :param root: Project root
    :param python: Interpreter, or ``None`` to find one
    :param test_command: A command line, or ``None`` for ``python -m pytest``
    :param src: Source root, or ``None`` to find it
    :returns: The project
    """
    py = find_python(root, python)
    cmd = shlex.split(test_command) if test_command else [py, "-m", "pytest"]
    return Project(root, py, cmd, find_src_root(root, src))


def run(cmd: list[str], cwd: Path, env: dict[str, str], timeout: float | None) -> Proc:
    """
    Run a command in its own process group; on timeout kill the whole group.

    :param cmd: Argument list
    :param cwd: Working folder
    :param env: Environment
    :param timeout: Seconds, or ``None``
    :returns: What happened
    """
    start = time.monotonic()
    proc = subprocess.Popen(  # nosec B603: the project's own test command, no shell
        cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
        return Proc(proc.returncode, time.monotonic() - start, False, out.decode("utf-8", errors="replace"))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, _ = proc.communicate()
        return Proc(-9, time.monotonic() - start, True, out.decode("utf-8", errors="replace"))


def module_names(paths: Iterable[str], src_root: str) -> list[str]:
    """
    Top level importable names of changed source files.

    :param paths: Paths relative to the project root
    :param src_root: The source root
    :returns: Sorted unique names
    """
    names = set()
    prefix = "" if src_root == "." else src_root.rstrip("/") + "/"
    for p in paths:
        rel = p[len(prefix) :] if p.startswith(prefix) else p.removeprefix("src/")
        first = rel.split("/", 1)[0]
        name = first[:-3] if first.endswith(".py") else first
        if name.isidentifier() and name not in {"setup", "conftest", "noxfile"}:
            names.add(name)
    return sorted(names)


def import_check(project: Project, tree: Path, names: list[str]) -> None:
    """
    Make sure the project's Python imports the changed packages from the worktree, not an installed copy.

    An editable install of the original checkout would otherwise shadow every mutant, and all would "survive".

    :param project: The project
    :param tree: A worktree
    :param names: Package or module names
    :raises EnvironmentProblem: When a name resolves outside the worktree
    """
    if not names:
        return
    code = (
        "import importlib.util, sys\n"
        "for n in sys.argv[1:]:\n"
        "    s = importlib.util.find_spec(n)\n"
        "    print(n, (s.origin or (list(s.submodule_search_locations or []) or [''])[0]) if s else '')\n"
    )
    proc = run([project.python, "-c", code, *names], tree, project.env(tree), 60)
    if proc.code != 0:
        raise EnvironmentProblem(f"import check failed: {project.python} could not run\n{proc.output.strip()}")
    real_tree = os.path.realpath(tree)
    for line in proc.output.splitlines():
        name, _, origin = line.partition(" ")
        if not origin:
            raise EnvironmentProblem(f"import check: {name} cannot be imported by {project.python}")
        if not os.path.realpath(origin).startswith(real_tree + os.sep):
            raise EnvironmentProblem(
                f"import check: {name} imports from {origin}, outside the worktree, so mutants would not be "
                "tested. Uninstall the editable install, or set [tool.ruru] src to the folder the package lives in."
            )


def has_pytest_cov(project: Project, tree: Path) -> bool:
    """
    Whether pytest-cov is installed for the project's interpreter.

    :param project: The project
    :param tree: A worktree
    :returns: True when it imports
    """
    return run([project.python, "-c", "import pytest_cov"], tree, project.env(tree), 60).code == 0


def _numbits_lines(numbits: bytes) -> list[int]:
    return [i * 8 + b for i, byte in enumerate(numbits) for b in range(8) if byte & (1 << b)]


def read_contexts(db: Path, tree: Path) -> dict[tuple[str, int], set[str]]:
    """
    Map (file, line) to the test node ids that executed it, from a coverage database with test contexts.

    :param db: The ``.coverage`` file
    :param tree: The worktree the run measured, to make paths relative
    :returns: Lines to node ids
    """
    out: dict[tuple[str, int], set[str]] = {}
    real = os.path.realpath(tree)
    con = sqlite3.connect(db)
    try:
        files = {fid: path for fid, path in con.execute("select id, path from file")}
        ctx = {cid: c for cid, c in con.execute("select id, context from context")}
        tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}

        def add(fid: int, cid: int, line: int) -> None:
            node = ctx.get(cid, "").rsplit("|", 1)[0]
            if not node:
                return
            rel = os.path.relpath(os.path.realpath(files[fid]), real)
            out.setdefault((rel, line), set()).add(node)

        if "line_bits" in tables:
            for fid, cid, bits in con.execute("select file_id, context_id, numbits from line_bits"):
                for line in _numbits_lines(bits):
                    add(fid, cid, line)
        if "arc" in tables:
            for fid, cid, a, b in con.execute("select file_id, context_id, fromno, tono from arc"):
                for line in (a, b):
                    if line > 0:
                        add(fid, cid, line)
    finally:
        con.close()
    return out


@dataclass
class Baseline:
    """The head's test run before any mutant."""

    seconds: float
    contexts: dict[tuple[str, int], set[str]] | None = None
    note: str = ""


def baseline(project: Project, tree: Path, use_coverage: bool) -> Baseline:
    """
    Run the whole suite once at the head; it must pass. With pytest-cov, record which tests run which lines.

    :param project: The project
    :param tree: A worktree at the head
    :param use_coverage: Try coverage based test selection
    :returns: The baseline
    :raises EnvironmentProblem: When the suite fails
    """
    env = project.env(tree)
    extra: list[str] = ["-q", "-p", "no:cacheprovider"]
    db = None
    if use_coverage and has_pytest_cov(project, tree):
        db = Path(tempfile.mkdtemp(prefix="ruru-cov-")) / ".coverage"
        env["COVERAGE_FILE"] = str(db)
        src = str(tree / project.src_root)
        extra += [f"--cov={src}", "--cov-context=test", "--cov-report=", "--no-cov-on-fail"]
    proc = run(project.command + extra, tree, env, None)
    if proc.code not in (0, 5):
        raise EnvironmentProblem(f"the baseline test run fails at the head (exit {proc.code}):\n{proc.output[-3000:]}")
    if db is not None and db.exists():
        try:
            return Baseline(proc.seconds, read_contexts(db, tree))
        except sqlite3.Error as exc:
            return Baseline(proc.seconds, None, f"coverage data unreadable ({exc}); every mutant runs the full suite")
    note = "" if db is not None else "pytest-cov not installed: every mutant runs the full suite"
    return Baseline(proc.seconds, None, note)


@dataclass
class Result:
    """The outcome of one mutant."""

    mutant: Mutant
    outcome: str
    tests: list[str] = field(default_factory=list)
    seconds: float = 0.0


def tests_for(m: Mutant, contexts: dict[tuple[str, int], set[str]] | None) -> list[str] | None:
    """
    The tests that execute a mutant's lines.

    :param m: The mutant
    :param contexts: Coverage contexts, or ``None`` when there are none
    :returns: Node ids, an empty list when no test runs the line, ``None`` for the full suite
    """
    if contexts is None:
        return None
    ids: set[str] = set()
    for line in range(m.line, m.end_line + 1):
        ids |= contexts.get((m.file, line), set())
    return sorted(ids)


def run_mutants(
    project: Project,
    trees: list[Path],
    mutants: list[Mutant],
    base: Baseline,
    deadline: float,
    on_result: Callable[[Result], None] | None = None,
) -> list[Result]:
    """
    Run each mutant against its tests, in parallel across the worktrees.

    :param project: The project
    :param trees: One worktree at the head per worker
    :param mutants: Mutants to run
    :param base: The baseline
    :param deadline: ``time.monotonic()`` after which the rest are skipped
    :param on_result: Called with each result as it lands
    :returns: Results in the order of ``mutants``
    """
    timeout = max(10.0, 5 * base.seconds)
    free: queue.Queue[Path] = queue.Queue()
    for t in trees:
        free.put(t)
    results: dict[int, Result] = {}

    def one(i: int, m: Mutant) -> Result:
        selected = tests_for(m, base.contexts)
        if selected == []:
            return Result(m, NOT_COVERED)
        if time.monotonic() > deadline:
            return Result(m, SKIPPED, selected or [])
        tree = free.get()
        target = tree / m.file
        original = target.read_bytes()
        try:
            target.write_text(m.source, encoding="utf-8")
            cmd = project.command + ["-x", "-q", "-p", "no:cacheprovider", *(selected or [])]
            proc = run(cmd, tree, project.env(tree), timeout)
        finally:
            target.write_bytes(original)
            free.put(tree)
        if proc.timed_out:
            outcome = TIMEOUT
        elif proc.code == 0 or proc.code == 5:
            outcome = SURVIVED
        else:
            outcome = KILLED
        return Result(m, outcome, selected or ["(full suite)"], round(proc.seconds, 2))

    with ThreadPoolExecutor(max_workers=len(trees)) as pool:
        futures = {pool.submit(one, i, m): i for i, m in enumerate(mutants)}
        for fut, i in futures.items():
            res = fut.result()
            results[i] = res
            if on_result:
                on_result(res)
    return [results[i] for i in range(len(mutants))]


def junit_outcomes(xml_path: Path, messages: dict[str, str] | None = None) -> dict[str, str]:
    """
    Read a JUnit XML report into outcomes keyed by ``classname::name`` without parameters.

    A parametrized test fails if any of its cases fails.

    :param xml_path: The report
    :param messages: Filled with the failure text per test, when given
    :returns: ``passed``, ``failed``, ``error`` or ``skipped`` per test
    """
    out: dict[str, str] = {}
    if not xml_path.exists():
        return out
    rank = {"passed": 0, "skipped": 1, "failed": 2, "error": 3}
    for case in ET.parse(xml_path).iter("testcase"):  # nosec B314: pytest's own report
        key = f"{case.get('classname', '')}::{case.get('name', '').split('[', 1)[0]}"
        state = "passed"
        for child in case:
            if messages is not None and child.tag in ("failure", "error"):
                messages[key] = messages.get(key, "") + (child.get("message") or "") + (child.text or "")
            if child.tag in ("failure",):
                state = "failed"
            elif child.tag == "error":
                state = "error"
            elif child.tag == "skipped":
                state = "skipped"
        if key not in out or rank[state] > rank[out[key]]:
            out[key] = state
    return out


def junit_key(node_id: str) -> str:
    """
    Give the JUnit key pytest writes for a node: ``tests/test_a.py::TestX::test_y`` to ``tests.test_a.TestX::test_y``.

    :param node_id: A pytest node id
    :returns: Its key
    """
    path, *rest = node_id.split("::")
    mod = path[:-3] if path.endswith(".py") else path
    mod = mod.replace("/", ".")
    classname = ".".join([mod, *rest[:-1]])
    return f"{classname}::{rest[-1] if rest else ''}"


def run_selected(
    project: Project, tree: Path, node_ids: list[str], timeout: float | None, messages: bool = False
) -> tuple[Proc, dict[str, str], dict[str, str]]:
    """
    Run some tests with a JUnit report.

    :param project: The project
    :param tree: The folder to run in
    :param node_ids: Pytest node ids
    :param timeout: Seconds
    :param messages: Also collect failure messages
    :returns: The run, the outcome per node id (missing when the test never ran), and failure text per node id
    """
    xml = Path(tempfile.mkdtemp(prefix="ruru-junit-")) / "junit.xml"
    cmd = project.command + ["-q", "-p", "no:cacheprovider", f"--junitxml={xml}", *node_ids]
    proc = run(cmd, tree, project.env(tree), timeout)
    texts: dict[str, str] = {}
    found = junit_outcomes(xml, texts if messages else None)
    outcomes = {n: found[junit_key(n)] for n in node_ids if junit_key(n) in found}
    return proc, outcomes, {n: texts[junit_key(n)] for n in node_ids if junit_key(n) in texts}


def python_version(python: str) -> str:
    """
    Read the version of an interpreter, for reports.

    :param python: Interpreter
    :returns: Such as ``3.12.4``, or ``unknown``
    """
    try:
        proc = subprocess.run(  # nosec B603: the project's interpreter, fixed arguments
            [python, "-c", "import platform; print(platform.python_version())"], capture_output=True, timeout=30
        )
        return proc.stdout.decode().strip() or "unknown"
    except OSError, subprocess.SubprocessError:
        return "unknown"
