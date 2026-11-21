"""
Mutants on the changed lines: small deliberate bugs, made from the AST and written back into the source text.

``col_offset`` values in the AST are UTF-8 byte offsets, so every edit slices the encoded source, never characters.
Nodes that rarely matter (logging, docstrings, ``__repr__`` and more, after Google's "arid nodes") are never mutated.
"""

import ast
import fnmatch
import hashlib
import random
import re
from collections.abc import Iterator
from dataclasses import asdict, dataclass

# Most valuable first: a run over budget keeps these.
OPERATORS = ["ROR", "NEG", "RET", "AOR", "LCR", "CRP", "AUG", "BRK", "SDL"]
RANK = {op: i for i, op in enumerate(OPERATORS)}

_BIN_SWAP: dict[type[ast.operator], type[ast.operator]] = {
    ast.Add: ast.Sub,
    ast.Sub: ast.Add,
    ast.Mult: ast.Div,
    ast.Div: ast.Mult,
    ast.FloorDiv: ast.Div,
    ast.Mod: ast.Mult,
    ast.Pow: ast.Mult,
}
_AUG_SWAP: dict[type[ast.operator], type[ast.operator]] = {
    ast.Add: ast.Sub,
    ast.Sub: ast.Add,
    ast.Mult: ast.Div,
    ast.Div: ast.Mult,
}
_CMP_SWAP: dict[type[ast.cmpop], type[ast.cmpop]] = {
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
}
_LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log", "fatal"}
_PRAGMA = re.compile(r"#\s*ruru:\s*ignore(?:\[([A-Z,\s]+)\])?")

HINTS = {
    "AOR": "assert the exact result of this arithmetic, with operands where + and - (or * and /) differ",
    "AUG": "assert the value after this update, not only that it changed",
    "ROR": "add a test at the boundary of this comparison, where < and <= (or == and !=) give different answers",
    "LCR": "add a case where exactly one side of this condition is true",
    "NEG": "add a test that takes the other branch of this condition",
    "CRP": "assert the value this constant produces, not just its type or truthiness",
    "RET": "assert the return value of this function, not only that it runs",
    "SDL": "assert the effect of this call: what it writes, sends, or changes",
    "BRK": "add a case where the loop meets this break or continue and later items matter",
}


@dataclass
class Mutant:
    """One mutant: where it is, what changed, and its full mutated source."""

    id: str
    file: str
    line: int
    end_line: int
    col: int
    operator: str
    before: str
    after: str
    source: str
    hint: str

    def describe(self) -> dict[str, object]:
        """
        Describe the mutant without its full source, for reports.

        :returns: A JSON ready dict
        """
        d = asdict(self)
        del d["source"]
        return d


class _Source:
    """The encoded source with byte offsets per line, so AST positions map to bytes."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.data = text.encode("utf-8")
        self.starts = [0]
        for i, b in enumerate(self.data):
            if b == 0x0A:
                self.starts.append(i + 1)
        self.lines = text.splitlines()

    def offset(self, line: int, col: int) -> int:
        return self.starts[line - 1] + col

    def span(self, node: ast.AST) -> tuple[int, int]:
        start = self.offset(node.lineno, node.col_offset)  # type: ignore[attr-defined]
        end = self.offset(node.end_lineno, node.end_col_offset)  # type: ignore[attr-defined]
        return start, end

    def segment(self, node: ast.AST) -> str:
        a, b = self.span(node)
        return self.data[a:b].decode("utf-8")

    def replace(self, node: ast.AST, new: str) -> str:
        a, b = self.span(node)
        return (self.data[:a] + new.encode("utf-8") + self.data[b:]).decode("utf-8")


def _ignored_ops(line: str) -> set[str] | None:
    """
    Read a ``# ruru: ignore`` pragma.

    :param line: Source line
    :returns: ``None`` when there is no pragma, an empty set for all operators, else the named ones
    """
    m = _PRAGMA.search(line)
    if not m:
        return None
    return {p.strip() for p in m[1].split(",") if p.strip()} if m[1] else set()


def _call_name(node: ast.Call) -> str:
    """
    Dotted name of what a call calls, as far as it is a plain name chain.

    :param node: The call
    :returns: Such as ``logger.info`` or ``print``
    """
    parts: list[str] = []
    f: ast.expr = node.func
    while isinstance(f, ast.Attribute):
        parts.append(f.attr)
        f = f.value
    if isinstance(f, ast.Name):
        parts.append(f.id)
    return ".".join(reversed(parts))


def _is_arid_call(node: ast.Call, patterns: list[str]) -> bool:
    name = _call_name(node)
    last = name.rsplit(".", 1)[-1]
    if name in {"print", "warnings.warn", "breakpoint"}:
        return True
    if last in _LOG_METHODS and ("log" in name.lower() or name.startswith("logging.")):
        return True
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def _is_main_guard(node: ast.If) -> bool:
    t = node.test
    return (
        isinstance(t, ast.Compare)
        and isinstance(t.left, ast.Name)
        and t.left.id == "__name__"
        and len(t.comparators) == 1
        and isinstance(t.comparators[0], ast.Constant)
        and t.comparators[0].value == "__main__"
    )


def _is_arid_stmt(node: ast.stmt, patterns: list[str]) -> bool:
    if isinstance(node, (ast.Assert, ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal)):
        return True
    if isinstance(node, ast.If) and _is_main_guard(node):
        return True
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {"__repr__", "__str__"}:
        return True
    if isinstance(node, ast.Raise) and node.exc is not None:
        exc = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
        if isinstance(exc, ast.Name) and exc.id == "NotImplementedError":
            return True
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(t, ast.Name) and t.id in {"__all__", "__version__"} for t in targets):
            return True
    if isinstance(node, ast.Expr):
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return True  # a docstring or a bare string
        if isinstance(node.value, ast.Call) and _is_arid_call(node.value, patterns):
            return True
    return False


class _Collector:
    """Walk a module and yield (operator, node, replacement text) for each mutation on a changed line."""

    def __init__(self, src: _Source, changed: set[int], patterns: list[str], operators: set[str]) -> None:
        self.src = src
        self.changed = changed
        self.patterns = patterns
        self.operators = operators

    def touches(self, node: ast.AST) -> bool:
        return any(n in self.changed for n in range(node.lineno, node.end_lineno + 1))  # type: ignore[attr-defined]

    def allowed(self, op: str, node: ast.AST) -> bool:
        if op not in self.operators or not self.touches(node):
            return False
        for n in range(node.lineno, node.end_lineno + 1):  # type: ignore[attr-defined]
            ign = _ignored_ops(self.src.lines[n - 1]) if n <= len(self.src.lines) else None
            if ign is not None and (not ign or op in ign):
                return False
        return True

    def walk(self, node: ast.AST) -> Iterator[tuple[str, ast.AST, str]]:
        if isinstance(node, ast.stmt) and _is_arid_stmt(node, self.patterns):
            return
        if isinstance(node, ast.Call) and _is_arid_call(node, self.patterns):
            return
        if isinstance(node, ast.JoinedStr):
            for v in node.values:
                if isinstance(v, ast.FormattedValue):
                    yield from self.walk(v.value)
            return
        yield from self.mutate(node)
        for name, value in ast.iter_fields(node):
            if name in {"annotation", "returns", "type_comment", "type_params", "decorator_list"}:
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) and name == "args":
                continue  # default values and annotations of parameters
            if isinstance(value, ast.AST):
                yield from self.walk(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, ast.AST):
                        yield from self.walk(item)

    def mutate(self, node: ast.AST) -> Iterator[tuple[str, ast.AST, str]]:
        seg = self.src.segment
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN_SWAP and self.allowed("AOR", node):
            new = ast.BinOp(node.left, _BIN_SWAP[type(node.op)](), node.right)
            yield "AOR", node, f"({ast.unparse(new)})"
        elif isinstance(node, ast.AugAssign) and type(node.op) in _AUG_SWAP and self.allowed("AUG", node):
            new_aug = ast.AugAssign(node.target, _AUG_SWAP[type(node.op)](), node.value)
            yield "AUG", node, ast.unparse(new_aug)
        elif isinstance(node, ast.Compare) and self.allowed("ROR", node):
            for i, op in enumerate(node.ops):
                if type(op) in _CMP_SWAP:
                    ops = list(node.ops)
                    ops[i] = _CMP_SWAP[type(op)]()
                    yield "ROR", node, f"({ast.unparse(ast.Compare(node.left, ops, node.comparators))})"
        elif isinstance(node, ast.BoolOp) and self.allowed("LCR", node):
            other = ast.Or() if isinstance(node.op, ast.And) else ast.And()
            yield "LCR", node, f"({ast.unparse(ast.BoolOp(other, node.values))})"
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not) and self.allowed("NEG", node):
            yield "NEG", node, f"({seg(node.operand)})"
        elif isinstance(node, ast.Constant) and self.allowed("CRP", node):
            v = node.value
            if isinstance(v, bool):
                yield "CRP", node, repr(not v)
            elif isinstance(v, int):
                yield "CRP", node, {0: "1", 1: "0"}.get(v, repr(v + 1))
            elif isinstance(v, str) and v:
                yield "CRP", node, '""'
        elif isinstance(node, (ast.If, ast.While)) and self.allowed("NEG", node.test):
            if not (isinstance(node.test, ast.UnaryOp) and isinstance(node.test.op, ast.Not)):
                yield "NEG", node.test, f"not ({seg(node.test)})"
        elif isinstance(node, ast.Return) and node.value is not None and self.allowed("RET", node):
            if not (isinstance(node.value, ast.Constant) and node.value.value is None):
                yield "RET", node, "return None"
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and self.allowed("SDL", node):
            yield "SDL", node, "pass"
        elif isinstance(node, ast.Break) and self.allowed("BRK", node):
            yield "BRK", node, "continue"
        elif isinstance(node, ast.Continue) and self.allowed("BRK", node):
            yield "BRK", node, "break"


def generate(
    path: str,
    text: str,
    changed: set[int],
    operators: list[str] | None = None,
    arid: list[str] | None = None,
) -> list[Mutant]:
    """
    Make every mutant of a source file that touches a changed line.

    :param path: Path relative to the repository root, for reports
    :param text: The file's source at the head
    :param changed: Added or modified line numbers
    :param operators: Operator codes to use; all when ``None``
    :param arid: Extra call name patterns never to mutate, such as ``metrics.*``
    :returns: Unique mutants that parse, in source order
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    src = _Source(text)
    collector = _Collector(src, changed, list(arid or []), set(operators or OPERATORS))
    seen: set[str] = set()
    out: list[Mutant] = []
    for op, node, new in collector.walk(tree):
        mutated = src.replace(node, new)
        try:
            ast.parse(mutated)
        except SyntaxError:
            continue
        digest = hashlib.sha256(mutated.encode("utf-8")).hexdigest()
        if digest in seen or mutated == text:
            continue
        seen.add(digest)
        line: int = node.lineno  # type: ignore[attr-defined]
        out.append(
            Mutant(
                id=f"{op}-{hashlib.sha256(f'{path}:{digest}'.encode()).hexdigest()[:10]}",
                file=path,
                line=line,
                end_line=node.end_lineno,  # type: ignore[attr-defined]
                col=node.col_offset,  # type: ignore[attr-defined]
                operator=op,
                before=src.segment(node),
                after=new,
                source=mutated,
                hint=HINTS[op],
            )
        )
    out.sort(key=lambda m: (m.line, m.col, RANK[m.operator], m.id))
    return out


def select(mutants: list[Mutant], limit: int, seed: str) -> tuple[list[Mutant], list[Mutant]]:
    """
    Keep at most ``limit`` mutants, the most valuable operators first, ties broken by a seeded shuffle.

    The seed is the head SHA, so reruns on the same commit pick the same mutants.

    :param mutants: All mutants
    :param limit: How many to keep
    :param seed: Seed for the shuffle
    :returns: The kept mutants in source order, and the rest
    """
    if len(mutants) <= limit:
        return list(mutants), []
    order = sorted(mutants, key=lambda m: m.id)
    random.Random(seed).shuffle(order)  # nosec B311: deterministic sampling, not security
    order.sort(key=lambda m: RANK[m.operator])
    keep = set(id(m) for m in order[:limit])
    kept = [m for m in mutants if id(m) in keep]
    rest = [m for m in mutants if id(m) not in keep]
    return kept, rest
