"""Small AST helpers shared by the fail first check and the tamper audit."""

import ast
import fnmatch
from pathlib import PurePosixPath

TEST_GLOBS = ["test_*.py", "*_test.py", "tests.py"]


def is_test_file(path: str, test_dirs: list[str]) -> bool:
    """
    Whether a path is a test module (``conftest.py`` is not: it is harness).

    :param path: Path relative to the root
    :param test_dirs: Folder names that hold tests
    :returns: True for test modules
    """
    p = PurePosixPath(path)
    if p.suffix != ".py" or p.name == "conftest.py":
        return False
    if any(fnmatch.fnmatchcase(p.name, g) for g in TEST_GLOBS):
        return True
    return any(part in test_dirs for part in p.parts[:-1]) and p.name != "__init__.py"


def is_source_file(path: str, test_dirs: list[str]) -> bool:
    """
    Whether a path is source code ruru may mutate.

    :param path: Path relative to the root
    :param test_dirs: Folder names that hold tests
    :returns: True for Python source outside tests and harness files
    """
    p = PurePosixPath(path)
    if p.suffix != ".py" or is_test_file(path, test_dirs):
        return False
    if p.name in {"conftest.py", "setup.py", "noxfile.py", "sitecustomize.py", "usercustomize.py"}:
        return False
    return not any(part in test_dirs or part.startswith(".") for part in p.parts[:-1])


def parse(text: str | None) -> ast.Module | None:
    """
    Parse source, or give ``None`` for missing or broken files.

    :param text: Source text
    :returns: The module
    """
    if text is None:
        return None
    try:
        return ast.parse(text)
    except SyntaxError:
        return None


def test_functions(tree: ast.Module | None) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """
    Collect test functions by qualified name: ``test_x`` or ``TestY::test_x``.

    :param tree: A parsed test module
    :returns: Functions keyed by qualified name
    """
    out: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    if tree is None:
        return out

    def visit(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                out[prefix + node.name] = node
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                visit(node.body, f"{prefix}{node.name}::")

    visit(tree.body, "")
    return out


def dump(node: ast.AST) -> str:
    """
    Dump a node without positions, so moving a function does not count as changing it.

    :param node: Any node
    :returns: Its normalised dump
    """
    return ast.dump(node, include_attributes=False)


def dotted(node: ast.expr) -> str:
    """
    Read the dotted name of a name or attribute chain, no call: ``pytest.mark.skip(...)`` to ``pytest.mark.skip``.

    :param node: An expression
    :returns: The name, or an empty string
    """
    if isinstance(node, ast.Call):
        node = node.func
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""
