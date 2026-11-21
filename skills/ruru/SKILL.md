---
name: ruru
description: Check your own Python change before you say it is done. Use after writing or changing Python code and its tests, before committing, before opening a pull request, or when asked to prove that tests really cover a change. ruru runs fail first (new tests must fail without the change), mutants on the changed lines (would the tests catch a planted bug?) and a tamper audit (were tests removed, loosened or skipped?).
---

# ruru

ruru reviews a change the way a strict reviewer would, with evidence. It calls no model; everything it reports was
measured by running the project's own tests.

## Before you say a change is done

1. Run the check. With the ruru MCP server connected, call `ruru_check` with the repository path. Otherwise:

   ```sh
   ruru check --json ruru-report.json    # the working tree against main; --base names another branch
   ```

2. Work the to do list, most serious first:
   - **Tamper findings**: put the removed or loosened assertion, the deleted test or the skipped test back. A test
     that is wrong gets fixed, not weakened.
   - **Survived mutants**: ruru changed one thing in your code (for example `<` to `<=`) and every test still
     passed. Write a test that fails with that change and passes without it. The `hint` says what kind of test.
     `ruru_explain` (or the report's `before` and `after`) shows the exact line.
   - **Not covered**: no test runs that line. Add one that does.
   - **Passes without the change**: the new test also passes on the base branch, so it proves nothing about your
     change. Assert what the change adds.
3. Verify each survivor: `ruru_verify` with its id, or `ruru verify <id>`. Killed means your test catches it.
4. Run `ruru_check` again until the gate passes.

## Rules

- Never weaken, skip, delete or wrap a test in `try` to make ruru pass. ruru reports exactly that.
- Never add `# ruru: ignore` to dodge a real gap. It is for code where the mutant cannot matter, such as an
  equivalent mutant; say why in the same comment.
- Exit code 2 means ruru could not test the project (the import check or the baseline failed). Read the message and
  fix the environment; do not report the change as checked.

`ruru guide` prints the full manual.
