# ruru

A pull request reviewer for code written by AI agents. Work in progress toward 1.0.0; the full README, the GitHub
Action and the agent plugin come in later milestones.

ruru asks three questions of a change, with evidence rather than opinion:

1. **Tamper audit.** Did the change weaken or bypass the tests: removed or loosened assertions, new skips, harness and
   CI edits?
2. **Fail first.** Do the new and changed tests fail without the change?
3. **Mutants on the diff.** Would the tests catch a small bug in the lines that changed?

Every check is deterministic. No model calls, no API keys, and the code never leaves the machine or the CI runner.

```sh
ruru check --base main --json ruru-report.json   # all three checks; the working tree when --head is left out
ruru audit                                       # the static tamper audit only
ruru mutants                                     # list the mutants without running them
ruru verify ROR-822e815b28                       # rerun one mutant after writing a test for it
ruru guide                                       # the full manual
```

The manual is [docs/MANUAL.md](docs/MANUAL.md). ruru needs Python 3.14 or later; the projects it checks can use any
Python from 3.9 on, through their own interpreter.

## Licence

MIT
