"""
A Model Context Protocol server over stdio, standard library only: the agent loop for ruru.

The agent calls ``ruru_check`` before it says a change is done, reads the survivors, writes a test for each, and
calls ``ruru_verify`` until the mutant is killed. Each message is one line of JSON-RPC 2.0. Nothing a client sends
ends the process; a bad argument becomes an error the agent can read.
"""

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from ruru_review import __version__, engine, gitio, mutants, pyast, runner

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = (
    "ruru checks a change before you call it done. Call ruru_check on the repository; for each survived mutant write "
    "a test that fails with the mutant and passes without it, then ruru_verify it. Fix tests that pass without the "
    "change. Never weaken, skip or delete a test to make ruru pass."
)
INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32600, -32601, -32602, -32603
Json = dict[str, Any]

_last: dict[str, Json] = {}


def _result(data: Json | str, error: bool = False) -> Json:
    if isinstance(data, str):
        return {"content": [{"type": "text", "text": data}], "isError": error}
    return {
        "content": [{"type": "text", "text": json.dumps(data, indent=1)}],
        "structuredContent": data,
        "isError": error,
    }


def _opts(args: Json) -> engine.Options:
    repo = args.get("repo", ".")
    if not isinstance(repo, str):
        raise ValueError("repo must be a path")
    opts = engine.Options(repo=Path(repo).expanduser())
    for key, kind in (("base", str), ("head", str), ("python", str), ("test_command", str), ("mode", str)):
        v = args.get(key)
        if v is not None:
            if not isinstance(v, kind):
                raise ValueError(f"{key} must be a string")
            setattr(opts, key, v)
    if opts.mode not in ("advise", "gate", "auto"):
        raise ValueError("mode must be advise, gate or auto")
    for key in ("max_mutants", "time_budget"):
        v = args.get(key)
        if v is not None:
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                raise ValueError(f"{key} must be a non negative number")
            setattr(opts, key, int(v) if key == "max_mutants" else float(v))
    return opts


def _brief(report: Json) -> Json:
    s = report["summary"]
    todo = [
        {k: i[k] for k in ("id", "check", "file", "line", "rule", "before", "after", "outcome", "hint")}
        for i in report["items"]
        if i["severity"] in ("high", "medium")
    ]
    return {
        "gate": s["gate"],
        "mutants": s["mutants"],
        "fail_first": s["fail_first"],
        "tamper": s["tamper"],
        "todo": todo,
    }


def tool_check(args: Json) -> Json:
    """
    Run all three checks.

    :param args: repo, base, head, mode, max_mutants, time_budget, python, test_command
    :returns: The gate, the counts and the to do list
    """
    opts = _opts(args)
    report = engine.review(opts)
    _last[str(gitio.toplevel(opts.repo))] = report
    return _result(_brief(report))


def tool_audit(args: Json) -> Json:
    """
    Run the static tamper audit only.

    :param args: repo, base, head
    :returns: Findings
    """
    opts = _opts(args)
    opts.run_tests, opts.max_mutants = False, 0
    report = engine.review(opts)
    return _result(
        {"tamper": report["summary"]["tamper"], "findings": [i for i in report["items"] if i["check"] == "tamper"]}
    )


def _report_for(args: Json) -> Json:
    root = str(gitio.toplevel(Path(str(args.get("repo", "."))).expanduser()))
    if root not in _last:
        raise ValueError("no report yet for this repository: call ruru_check first")
    return _last[root]


def tool_survivors(args: Json) -> Json:
    """
    List survived and not covered mutants from the last check.

    :param args: repo
    :returns: Items
    """
    report = _report_for(args)
    return _result({"survivors": [i for i in report["items"] if i["outcome"] in (runner.SURVIVED, runner.NOT_COVERED)]})


def tool_explain(args: Json) -> Json:
    """
    Show one item with the source around it and the tests that ran.

    :param args: repo, id
    :returns: The item and its context
    :raises ValueError: When the id is not in the last report
    """
    report = _report_for(args)
    iid = args.get("id")
    item = next((i for i in report["items"] if i["id"] == iid), None)
    if item is None:
        raise ValueError(f"no item {iid!r} in the last report")
    root = gitio.toplevel(Path(str(args.get("repo", "."))).expanduser())
    text = (
        (root / item["file"]).read_text(encoding="utf-8", errors="replace").splitlines()
        if (root / item["file"]).is_file()
        else []
    )
    lo, hi = max(1, item["line"] - 4), min(len(text), item["end_line"] + 4)
    context = "\n".join(
        f"{n:>5}{'>' if item['line'] <= n <= item['end_line'] else ' '} {text[n - 1]}" for n in range(lo, hi + 1)
    )
    return _result({"item": item, "context": context})


def tool_verify(args: Json) -> Json:
    """
    Rerun one mutant against the tests as they are now.

    :param args: repo, base, id
    :returns: The outcome
    :raises ValueError: When the id is not in the change
    """
    opts = _opts(args)
    ctx = engine.resolve(opts)
    iid = args.get("id")
    found: list[mutants.Mutant] = []
    for f in ctx.files:
        if f.new_path and f.added and pyast.is_source_file(f.new_path, ctx.cfg.tests):
            text = gitio.read_at(ctx.root, ctx.head, f.new_path)
            if text is not None:
                found += [
                    m
                    for m in mutants.generate(f.new_path, text, f.added, ctx.cfg.operators, ctx.cfg.arid)
                    if m.id == iid
                ]
    if not found:
        raise ValueError(f"no mutant {iid!r} in the change; the line may have moved: call ruru_check again")
    m = found[0]
    project = runner.make_project(ctx.root, opts.python, opts.test_command or ctx.cfg.test_command, ctx.cfg.src)
    with gitio.Worktree(ctx.root, ctx.head_sha, ctx.head is None) as tree:
        runner.import_check(project, tree, runner.module_names([m.file], project.src_root))
        base = runner.baseline(project, tree, use_coverage=False)
        res = runner.run_mutants(project, [tree], [m], base, float("inf"))[0]
    return _result(
        {
            "id": m.id,
            "file": m.file,
            "line": m.line,
            "outcome": res.outcome,
            "killed": res.outcome in (runner.KILLED, runner.TIMEOUT),
        }
    )


_REPO = {"repo": {"type": "string", "description": "repository folder (default: the server's working folder)"}}
_CHANGE = _REPO | {
    "base": {"type": "string", "description": "target branch"},
    "head": {"type": "string", "description": "head commit; leave out for the working tree"},
}
_RUN = _CHANGE | {
    "python": {"type": "string"},
    "test_command": {"type": "string"},
}
TOOLS: dict[str, tuple[Callable[[Json], Json], str, Json, list[str], bool]] = {
    "ruru_check": (
        tool_check,
        "Run the tamper audit, fail first and mutants on the change. Returns the gate and a to do list.",
        _RUN
        | {
            "mode": {"type": "string", "enum": ["advise", "gate", "auto"]},
            "max_mutants": {"type": "integer"},
            "time_budget": {"type": "number"},
        },
        [],
        False,
    ),
    "ruru_survivors": (
        tool_survivors,
        "List survived and not covered mutants from the last ruru_check.",
        _REPO,
        [],
        True,
    ),
    "ruru_explain": (
        tool_explain,
        "Show one finding or mutant with the source lines around it and the tests that ran.",
        _REPO | {"id": {"type": "string"}},
        ["id"],
        True,
    ),
    "ruru_verify": (
        tool_verify,
        "Rerun one mutant against the current tests: killed means your new test catches it.",
        _RUN | {"id": {"type": "string"}},
        ["id"],
        False,
    ),
    "ruru_audit": (
        tool_audit,
        "Run only the static tamper audit: removed, loosened or skipped tests.",
        _CHANGE,
        [],
        True,
    ),
}


def tools_list() -> list[Json]:
    """
    Describe the tools.

    :returns: MCP tool descriptions
    """
    out = []
    for name, (_, about, props, required, read_only) in TOOLS.items():
        out.append(
            {
                "name": name,
                "description": about,
                "inputSchema": {
                    "type": "object",
                    "properties": props,
                    "required": required,
                    "additionalProperties": False,
                },
                "annotations": {"readOnlyHint": read_only, "destructiveHint": False, "openWorldHint": False},
            }
        )
    return out


def handle(msg: Any) -> Json | None:
    """
    Answer one JSON-RPC message.

    :param msg: The parsed message
    :returns: The response, or ``None`` for a notification
    """
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": INVALID_REQUEST, "message": "invalid request"}}
    mid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
    if "id" not in msg:
        return None

    def ok(result: Json) -> Json:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def err(code: int, text: str) -> Json:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": text}}

    if method == "initialize":
        asked = params.get("protocolVersion") if isinstance(params, dict) else None
        version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return ok(
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "ruru", "version": __version__},
                "instructions": INSTRUCTIONS,
            }
        )
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": tools_list()})
    if method == "tools/call":
        if not isinstance(params, dict) or params.get("name") not in TOOLS:
            return err(INVALID_PARAMS, f"unknown tool; tools: {', '.join(TOOLS)}")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return err(INVALID_PARAMS, "arguments must be an object")
        func = TOOLS[params["name"]][0]
        try:
            return ok(func(args))
        except (ValueError, gitio.GitError, runner.EnvironmentProblem, OSError) as exc:
            return ok(_result(str(exc), error=True))
        except Exception as exc:  # noqa: B902  # the server must survive any tool failure
            return ok(_result(f"internal error: {type(exc).__name__}: {exc}", error=True))
    return err(METHOD_NOT_FOUND, f"unknown method {method}")


def serve(inp: TextIO = sys.stdin, out: TextIO = sys.stdout) -> int:
    """
    Serve until standard input closes.

    :param inp: Input stream
    :param out: Output stream
    :returns: Exit code
    """
    for line in inp:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply: Json | None = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            reply = handle(msg)
        if reply is not None:
            out.write(json.dumps(reply) + "\n")
            out.flush()
    return 0
