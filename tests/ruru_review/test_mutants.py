import ast

from ruru_review.mutants import OPERATORS, generate, select

ALL = set(range(1, 200))


def ops(text: str, changed: set[int] = ALL) -> list[tuple[str, str, str]]:
    return [(m.operator, m.before, m.after) for m in generate("m.py", text, changed)]


def test_each_operator() -> None:
    text = (
        "def f(a, b, xs):\n"
        "    a += 1\n"
        "    if a < b and xs:\n"
        "        g()\n"
        "    for x in xs:\n"
        "        if not x:\n"
        "            break\n"
        "        continue\n"
        "    return a * b == True\n"
    )
    found = {o for o, _, _ in ops(text)}
    assert found == set(OPERATORS)


def test_only_changed_lines() -> None:
    text = "def f(a):\n    x = a + 1\n    return a - 1\n"
    assert {(o, b) for o, b, _ in ops(text, {3})} == {("RET", "return a - 1"), ("AOR", "a - 1"), ("CRP", "1")}


def test_arid_nodes_are_skipped() -> None:
    text = (
        '"""doc"""\n'
        "import logging\n"
        "__version__ = '1'\n"
        "log = logging.getLogger(__name__)\n"
        "def f(a: int = 1) -> int:\n"
        "    log.info('x %s', a + 1)\n"
        "    print(a + 1)\n"
        "    assert a > 0\n"
        "    raise NotImplementedError('x')\n"
        "class C:\n"
        "    def __repr__(self):\n"
        "        return 'C' + str(1)\n"
        "if __name__ == '__main__':\n"
        "    f(1 + 1)\n"
    )
    assert ops(text) == []


def test_pragma_ignores() -> None:
    text = "def f(a):\n    return a + 1  # ruru: ignore[AOR]\n\ndef g(a):\n    return a - 1  # ruru: ignore\n"
    assert {o for o, _, _ in ops(text)} == {"RET", "CRP"}


def test_byte_offsets_with_non_ascii() -> None:
    text = 'def f(a):\n    s = "héllo wörld"; return a * 2\n'
    found = generate("m.py", text, {2})
    aor = [m for m in found if m.operator == "AOR"][0]
    assert aor.before == "a * 2"
    assert "return (a / 2)" in aor.source
    for m in found:
        ast.parse(m.source)


def test_f_string_text_not_mutated_but_values_are() -> None:
    assert ops('def f(a):\n    return f"total {a + 1}"\n', {2}) == [
        ("RET", 'return f"total {a + 1}"', "return None"),
        ("AOR", "a + 1", "(a - 1)"),
        ("CRP", "1", "0"),
    ]


def test_dedupe_and_parse() -> None:
    found = generate("m.py", "x = 0\n", {1})
    assert [(m.before, m.after) for m in found] == [("0", "1")]
    assert generate("m.py", "def (:\n", {1}) == []


def test_select_is_deterministic_and_ranked() -> None:
    text = "\n".join(f"def f{i}(a):\n    return a + {i} > 3" for i in range(20)) + "\n"
    found = generate("m.py", text, ALL)
    kept1, rest1 = select(found, 10, "abc")
    kept2, _ = select(found, 10, "abc")
    assert [m.id for m in kept1] == [m.id for m in kept2]
    assert len(kept1) == 10 and len(rest1) == len(found) - 10
    assert {m.operator for m in kept1} == {"ROR"}
    assert select(found, 999, "x")[1] == []
