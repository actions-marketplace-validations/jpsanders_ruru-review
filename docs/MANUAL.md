# ruru

A pull request reviewer for code written by AI agents. ruru asks three questions of a change and answers each with
evidence, not opinion:

1. **Tamper audit.** Did the change weaken or bypass the tests?
2. **Fail first.** Do the new and changed tests fail without the change?
3. **Mutants on the diff.** Would the tests catch a bug in the lines that changed?

Every check is deterministic. ruru calls no model, needs no API key, and your code never leaves the machine or the CI
runner. Your own coding agent reads what ruru finds (`--json`), writes the missing tests, and ruru verifies them.

## Synopsis

```text
ruru check   [--base REF] [--head REF] [--mode advise|gate|auto] [--json PATH] [--markdown PATH]
             [--max-mutants N] [--time-budget S] [--workers N] [--test-command CMD] [--python PATH]
             [--refactor] [--label NAME] [--quiet] [--json-stdout] [--repo PATH]
ruru audit   [--base REF] [--head REF] [--mode MODE] [--json PATH] [--markdown PATH]
ruru mutants [--base REF] [--head REF] [--json-stdout]
ruru verify  ID [--base REF] [--head REF] [--test-command CMD] [--python PATH]
ruru guide
ruru man [--install] [--raw]
ruru --version
```

## The change

The base is the merge base of the target branch and the head. The target branch is `--base`, then `[tool.ruru] base`,
then origin's HEAD, `main` or `master`. The head is `--head` (in CI, the pull request's head commit), or the working
tree when `--head` is left out, uncommitted and untracked files included, so an agent can check its work before it
commits.

## Commands

### check

Runs all three checks and prints a score card, then the findings, most serious first. Progress lines go to stderr.

- `--json PATH` writes the report an agent works from (see Report).
- `--markdown PATH` writes the summary as Markdown. Inside GitHub Actions the same summary is appended to the job
  summary.
- `--mode` picks what a failed gate does: `gate` fails the run, `advise` never fails it, `auto` (the default) gates
  changes written by an agent and advises on the rest.
- `--refactor` says the change is a refactor: tests should stay green on both sides, so fail first gives advice only.
- `--max-mutants N` (default 60) and `--time-budget S` (default 600) cap the mutant run. Over the cap, ruru keeps the
  most valuable operators first and breaks ties with a shuffle seeded by the head SHA, so reruns on the same commit
  choose the same mutants.
- `--workers N` (default 2) runs mutants in that many git worktrees at once.
- `--python PATH` is the project's interpreter. Without it ruru uses `.venv/bin/python`, `venv/bin/python`, the active
  virtualenv, then `python3`. The project can use any Python from 3.9 on; ruru itself needs 3.14.
- `--test-command CMD` replaces `PYTHON -m pytest`. It must accept pytest's arguments.
- `--label NAME` passes a pull request label for agent detection; repeat it for several.

### audit

Only the tamper audit. Static and fast: nothing is installed and no test runs.

### mutants

Lists the mutants of the change, with their ids, without running anything.

### verify ID

Reruns one mutant, by the id a report gave it, against the tests as they are now. Exit 0 when the tests kill it, 1
when it still survives, 2 when the id is not in the change (the line moved or changed; run `ruru mutants`).

### guide, man

`guide` prints this manual. `man` shows it as a man page; `man --install` puts it where `man ruru` finds it.

## The three checks

### Tamper audit

ruru compares the base and head versions of every changed test module, `conftest.py`, test and coverage
configuration, CI workflow and source file. Each finding is high severity unless it says otherwise.

| Rule | What it means |
|---|---|
| `test-removed` | a test function that existed on the base is gone |
| `assertion-removed` | a test still exists but asserts less |
| `assertion-loosened` | exact assertions became weaker: `==` to `assert x`, `is not None` or `in`; |
| | `assertEqual` to `assertTrue`; `raises(KeyError)` to `raises(Exception)` |
| `assert-true` | a test now asserts `True` |
| `approx-widened` | a `pytest.approx` tolerance grew |
| `cases-removed` | a `parametrize` list lost cases |
| `skip-added` | a new `skip`, `skipif`, `xfail`, `pytest.skip()`, `unittest.skip`, `importorskip` or `pytestmark` |
| `empty-test` | a new test asserts nothing and calls nothing |
| `assertion-in-try` | assertions were wrapped in `try`/`except` |
| `except-pass` | a test swallows an exception with `pass` |
| `harness-hook` | a `conftest.py` gained a hook that can change what runs, such as `pytest_collection_modifyitems` |
| `addopts-deselect` | test options gained `-k`, `--deselect`, `--ignore` or `-p no:` |
| `coverage-omit`, `coverage-lowered` | coverage leaves code out, or its floor went down |
| `startup-hook` | a new `sitecustomize.py` or `usercustomize.py` |
| `ci-ignores-failure` | a CI line gained `\|\| true` or `continue-on-error: true` |
| `no-cover-added` (medium) | source gained `# pragma: no cover` |

When a change touches only tests and weakens them, the summary says so (`tests_only_weakening`).

### Fail first

ruru finds the test functions the change adds or edits, comparing each function's syntax tree by its qualified name,
so a moved function is not a changed one. It runs them at the head, then in a worktree at the base with the head's test
files laid over it. Imports of names that do not exist on the base become placeholders that fail when used, so one new
name does not hide every other test in the file.

| Outcome | Meaning |
|---|---|
| `proves_change` | fails on the base, passes at the head |
| `needs_new_code` | fails on the base because it uses code the change adds |
| `passes_without_change` | passes on both sides: it does not prove the change (a finding) |
| `fails_on_head` | does not pass at the head; reported, not scored |
| `not_run` | skipped or not collected |

### Mutants on the diff

ruru parses each changed source file and mutates only syntax that touches an added or modified line. Each mutant is
one small bug:

| Code | Mutation |
|---|---|
| `ROR` | comparison: `<` and `<=`, `>` and `>=`, `==` and `!=`, `is` and `is not`, `in` and `not in` |
| `NEG` | condition: `if c` to `if not (c)`, `while c` likewise, an existing `not` removed |
| `RET` | `return x` to `return None` |
| `AOR` | arithmetic: `+` and `-`, `*` and `/`, `//` to `/`, `%` to `*`, `**` to `*` |
| `LCR` | boolean: `and` and `or` |
| `CRP` | constants: `n` to `n + 1`, `0` and `1` swapped, `True` and `False`, a non empty string to `""` |
| `AUG` | `+=` and `-=`, `*=` and `/=` |
| `BRK` | `break` and `continue` |
| `SDL` | a call statement replaced by `pass` |

**Never mutated** (arid code): logging and `print` calls, `warnings.warn`, docstrings, annotations and default
values, `__repr__` and `__str__`, `if __name__ == "__main__":` blocks, `assert` and imports in source,
`raise NotImplementedError`, the text of f strings, `__all__` and `__version__`. A line ending in `# ruru: ignore`
is skipped; `# ruru: ignore[ROR,CRP]` skips only those operators. `[tool.ruru] arid` adds call name patterns such as
`metrics.*`.

**How mutants run.** ruru makes one git worktree at the head per worker. Before any mutant it runs an **import check**:
the project's interpreter, with the worktree's `src` (or root) first on `PYTHONPATH`, must import each changed
package from the worktree. If an installed copy wins, every mutant would survive, so ruru stops with exit 2 and says
why. Then a **baseline** run of the whole suite must pass. With pytest-cov installed in the project, the baseline also
records which tests execute which lines, and each mutant runs only those tests; without it, each mutant runs the full
suite. Each mutant runs with `-x -q -p no:cacheprovider` and a timeout of 5 times the baseline (at least 10 seconds);
on timeout the whole process group is killed.

| Outcome | Meaning |
|---|---|
| `killed` | a test failed |
| `timeout` | the tests hung; counts as killed |
| `survived` | every selected test passed: a gap in the tests (a finding) |
| `not_covered` | no test executes the line (a finding, not scored) |
| `skipped` | over the mutant cap or the time budget |

The **score** is killed and timed out mutants over all that ran.

## Agents and the gate

A change counts as an agent's when its pull request author is a listed bot (`RURU_PR_AUTHOR`), when a commit carries
a listed co author trailer, when the branch starts with `claude/`, `copilot/`, `codex/`, `cursor/` or `devin/`
(`GITHUB_HEAD_REF` in Actions, else the current branch), or when it has an `agent` label (`--label`, or
`RURU_PR_LABELS`, comma separated). Every list lives in `[tool.ruru.agents]`.

The gate fails on any high tamper finding, on any new or changed test that passes without the change (unless
`--refactor`), or on a mutation score under 70% with at least 5 mutants run. In `auto` mode it is enforced for agent
changes only; for people it gives the same findings as advice.

## Report

`--json` writes schema `ruru.report/1`. `summary` holds the counts, the score and the gate's result and reasons.
`items` holds one entry per finding or mutant, most serious first:

```json
{
  "id": "ROR-822e815b28",
  "check": "mutant",
  "file": "src/calc/__init__.py",
  "line": 15,
  "end_line": 15,
  "rule": "ROR",
  "before": "pct > 100",
  "after": "(pct >= 100)",
  "tests": ["tests/test_calc.py::test_discount_runs"],
  "outcome": "survived",
  "severity": "high",
  "side": "RIGHT",
  "hint": "add a test at the boundary of this comparison, where < and <= (or == and !=) give different answers"
}
```

`check` is `tamper`, `fail_first` or `mutant`. Two runs on the same commit write the same report.

## The agent loop

Before an agent says a change is done:

1. `ruru check --json ruru-report.json`
2. For each `survived` item, write a test that fails with the mutant and passes without it; for each
   `passes_without_change`, assert what the change adds; for each tamper finding, put the test back.
3. `ruru verify <id>` for each survivor until it is killed.
4. Never weaken, skip or delete a test to make ruru pass.

## Configuration

In the checked project's `pyproject.toml`:

```toml
[tool.ruru]
base = "main"
test-command = "python -m pytest -p no:randomly"
src = "src"
tests = ["tests"]
max-mutants = 60
time-budget = 600
workers = 2
operators = ["ROR", "NEG", "RET", "AOR", "LCR", "CRP", "AUG", "BRK", "SDL"]
arid = ["metrics.*", "tracer.*"]

[tool.ruru.gate]
tamper-high = true
fail-first = true
min-score = 0.70
min-mutants = 5

[tool.ruru.agents]
bots = ["copilot-swe-agent[bot]", "claude[bot]", "devin-ai-integration[bot]", "cursor[bot]"]
branches = ["claude/", "copilot/", "codex/", "cursor/", "devin/"]
labels = ["agent"]
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | passed, or advice only |
| 1 | the gate failed |
| 2 | a usage or environment problem: not a repository, no base, the import check or the baseline failed |
