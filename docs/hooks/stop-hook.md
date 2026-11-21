# Opt in: block "done" while the gate fails

A Claude Code Stop hook runs when the agent tries to finish. This one runs `ruru check --mode gate` and, when the
gate fails, tells the agent why and keeps it working. Add it to `.claude/settings.json` in the project:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "out=$(ruru check --mode gate --quiet --max-mutants 30 --time-budget 240 2>&1); code=$?; if [ $code -eq 1 ]; then printf '%s\\n' \"$out\" >&2; exit 2; fi; exit 0",
            "timeout": 300
          }
        ]
      }
    ]
  }
}
```

Exit code 2 from a Stop hook blocks the stop and shows stderr to the agent. ruru's own exit code 2 (an environment
problem) is let through here, so a broken environment never traps the agent in a loop. Keep the budget small: the
hook runs every time the agent stops.
