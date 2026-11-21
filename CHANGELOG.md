# Changelog

Versions follow [semantic versioning](https://semver.org). Tags carry the bare version, such as `1.0.0`.

## Unreleased

- `ruru check`: the tamper audit, fail first and mutants on the diff, with a JSON report (schema `ruru.report/1`),
  a Markdown summary and the GitHub job summary.
- Fail first lays the head's test files over a worktree at the base; imports of names missing on the base become
  placeholders, so only the tests that use new code count as needing it.
- Mutants: nine operators on changed lines only, arid nodes never mutated, `# ruru: ignore` pragmas, sampling seeded
  by the head SHA so reruns agree.
- Runner: one git worktree per worker, an import check against installed copies shadowing the worktree, a baseline
  run, test selection by pytest-cov contexts, timeouts that kill the whole process group, a time budget.
- Agent detection by branch prefix, co author trailer, label or bot author; the gate enforces for agents and advises
  for people.
- `ruru audit`, `ruru mutants`, `ruru verify`, `ruru guide`, `ruru man`.
