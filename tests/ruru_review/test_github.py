import io
import json
from pathlib import Path
from typing import Any

import pytest

from ruru_review import github, mcp
from ruru_review.cli import main

SHA = "a" * 40


def report(**extra: Any) -> dict[str, Any]:
    items = [
        {
            "id": "ROR-1",
            "check": "mutant",
            "file": "src/a.py",
            "line": 3,
            "end_line": 3,
            "rule": "ROR",
            "before": "a < b",
            "after": "(a <= b)",
            "tests": ["tests/test_a.py::test_x"],
            "outcome": "survived",
            "severity": "high",
            "side": "RIGHT",
            "hint": "add a boundary test",
        },
        {
            "id": "t1",
            "check": "tamper",
            "file": "tests/test_a.py",
            "line": 9,
            "end_line": 9,
            "rule": "skip-added",
            "before": "",
            "after": "",
            "tests": [],
            "outcome": "finding",
            "severity": "high",
            "side": "RIGHT",
            "hint": "test_y gained a skip @someone <b>x</b> | pipe",
        },
        {
            "id": "ROR-2",
            "check": "mutant",
            "file": "src/a.py",
            "line": 50,
            "end_line": 50,
            "rule": "ROR",
            "before": "x",
            "after": "y",
            "tests": [],
            "outcome": "survived",
            "severity": "high",
            "side": "RIGHT",
            "hint": "h",
        },
        {
            "id": "k1",
            "check": "mutant",
            "file": "src/a.py",
            "line": 4,
            "end_line": 4,
            "rule": "AOR",
            "before": "a",
            "after": "b",
            "tests": [],
            "outcome": "killed",
            "severity": "info",
            "side": "RIGHT",
            "hint": "",
        },
        {"id": "bad/../id", "check": "mutant", "file": "../etc/passwd", "line": 1, "severity": "high"},
    ]
    r = {
        "schema": "ruru.report/1",
        "tool": {"version": "1.0.0"},
        "base": {"ref": "origin/main"},
        "head": {"sha": SHA},
        "summary": {
            "mutants": {"total": 3, "run": 3, "killed": 1, "survived": 2, "score": 0.33},
            "fail_first": {"tests": 1},
            "tamper": {"high": 1, "medium": 0},
            "gate": {"result": "fail", "reasons": ["1 high tamper finding(s)"]},
        },
        "items": items,
    }
    r.update(extra)
    return r


class FakeGitHub(github.GitHub):
    def __init__(self, head: str = SHA, existing: list[dict[str, Any]] | None = None, posted: str = "") -> None:
        super().__init__("t", "o/r")
        self.calls: list[tuple[str, str, Any]] = []
        self.head, self.existing, self.posted = head, existing or [], posted

    def call(self, method: str, path: str, body: Any = None) -> Any:
        self.calls.append((method, path, body))
        if path == "/pulls/7":
            return {"head": {"sha": self.head}}
        if path.startswith("/issues/7/comments"):
            return self.existing
        if path.startswith("/pulls/7/comments"):
            return [{"body": self.posted}] if self.posted else []
        if path.startswith("/pulls/7/files"):
            return [
                {"filename": "src/a.py", "patch": "@@ -1,2 +1,4 @@\n a\n+b\n+c\n d"},
                {"filename": "tests/test_a.py", "patch": "@@ -8,1 +8,2 @@\n x\n+@pytest.mark.skip"},
            ]
        return {}


def run_post(
    tmp_path: Path, gh: FakeGitHub, event: dict[str, Any], pr: int | None = None, data: Any = None
) -> dict[str, Any]:
    path = tmp_path / "r.json"
    path.write_text(json.dumps(data if data is not None else report()))
    return github.post(path, gh, event, pr, 5)


PR_EVENT = {"pull_request": {"number": 7, "head": {"sha": SHA}}}


def test_post_creates_comment_and_review(tmp_path: Path) -> None:
    gh = FakeGitHub()
    done = run_post(tmp_path, gh, PR_EVENT)
    assert done == {"pull_request": 7, "summary_comment": "created", "inline_comments": 2}
    review = [b for m, p, b in gh.calls if p == "/pulls/7/reviews"][0]
    assert review["commit_id"] == SHA and review["event"] == "COMMENT"
    assert [(c["path"], c["line"]) for c in review["comments"]] == [("tests/test_a.py", 9), ("src/a.py", 3)]
    body = review["comments"][0]["body"]
    assert "@​someone" in body and "<b>" not in body and "\\|" in body and "<!-- ruru:id=t1 -->" in body
    assert "```suggestion" not in json.dumps(review)


def test_post_updates_and_dedupes(tmp_path: Path) -> None:
    gh = FakeGitHub(existing=[{"id": 99, "body": "<!-- ruru -->\nold"}], posted="x <!-- ruru:id=t1 -->")
    done = run_post(tmp_path, gh, PR_EVENT)
    assert done["summary_comment"] == "updated" and done["inline_comments"] == 1
    assert ("PATCH", "/issues/comments/99") in [(m, p) for m, p, _ in gh.calls]


def test_workflow_run_checks_the_head(tmp_path: Path) -> None:
    event = {"workflow_run": {"head_sha": SHA, "pull_requests": []}}
    assert run_post(tmp_path, FakeGitHub(), event, pr=7)["pull_request"] == 7
    with pytest.raises(github.PostError, match="head is"):
        run_post(tmp_path, FakeGitHub(head="b" * 40), event, pr=7)
    with pytest.raises(github.PostError, match="names no pull request"):
        run_post(tmp_path, FakeGitHub(), event)
    with pytest.raises(github.PostError, match="another commit"):
        run_post(tmp_path, FakeGitHub(), PR_EVENT, data=report(head={"sha": "c" * 40}))
    with pytest.raises(github.PostError, match="pull_request or workflow_run"):
        run_post(tmp_path, FakeGitHub(), {"push": {}})


@pytest.mark.parametrize(
    "bad",
    [
        [],
        {"schema": "x"},
        {"schema": "ruru.report/1", "head": {"sha": "nope"}},
        {"schema": "ruru.report/1", "head": {"sha": SHA}},
    ],
)
def test_validate_rejects(bad: Any) -> None:
    with pytest.raises(github.PostError):
        github.validate(bad)


def test_validate_drops_bad_items_and_numbers() -> None:
    r = report()
    r["summary"]["mutants"]["killed"] = -5
    r["summary"]["mutants"]["score"] = 7
    clean = github.validate(r)
    assert [i["id"] for i in clean["items"]] == ["ROR-1", "t1", "ROR-2", "k1"]
    assert clean["summary"]["mutants"]["killed"] == 0 and clean["summary"]["mutants"]["score"] is None


def test_sarif_and_annotations() -> None:
    clean = github.validate(report())
    log = github.sarif(clean)
    assert log["version"] == "2.1.0" and len(log["runs"][0]["results"]) == 3
    assert {r["ruleId"] for r in log["runs"][0]["results"]} == {"mutant/ROR", "tamper/skip-added"}
    lines = list(github.annotations(clean))
    assert lines[0].startswith("::warning file=tests/test_a.py,line=9,title=ruru tamper skip-added::")


def test_commentable_sides() -> None:
    allowed = github.commentable([{"filename": "a.py", "patch": "@@ -3,2 +3,2 @@\n-x\n+y\n z"}])
    assert allowed == {("a.py", "LEFT", 3), ("a.py", "RIGHT", 3), ("a.py", "RIGHT", 4), ("a.py", "LEFT", 4)}


def test_bad_repo_name() -> None:
    with pytest.raises(github.PostError):
        github.GitHub("t", "o/r; rm")


def test_cli_post_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    assert main(["post", "--report", "x.json"]) == 2
    assert "GITHUB_TOKEN" in capsys.readouterr().err
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    assert main(["post", "--report", "x.json", "--repository", "o/r"]) == 2


def rpc(*msgs: Any) -> list[dict[str, Any]]:
    out = io.StringIO()
    mcp.serve(io.StringIO("\n".join(m if isinstance(m, str) else json.dumps(m) for m in msgs) + "\n"), out)
    return [json.loads(x) for x in out.getvalue().splitlines()]


def test_mcp_protocol() -> None:
    init, tools, bad, unknown, notif_free, garbage = rpc(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "nope"}},
        {"jsonrpc": "2.0", "id": 4, "method": "what"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 5, "method": "ping"},
        "{not json",
    )
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert [t["name"] for t in tools["result"]["tools"]] == [
        "ruru_check",
        "ruru_survivors",
        "ruru_explain",
        "ruru_verify",
        "ruru_audit",
    ]
    assert bad["error"]["code"] == -32602 and unknown["error"]["code"] == -32601
    assert notif_free["result"] == {} and garbage["error"]["code"] == -32700


def test_mcp_tools_end_to_end(make_repo: Any, py: str) -> None:
    repo = str(make_repo())
    (check,) = rpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "ruru_check", "arguments": {"repo": repo, "python": py, "head": "HEAD"}},
        }
    )
    got = check["result"]["structuredContent"]
    assert got["gate"]["result"] == "fail" and got["todo"]
    mcp._last.clear()
    msgs = [
        {"name": "ruru_check", "arguments": {"repo": repo, "python": py}},
        {"name": "ruru_survivors", "arguments": {"repo": repo}},
        {"name": "ruru_audit", "arguments": {"repo": repo}},
        {"name": "ruru_check", "arguments": {"repo": repo, "mode": "bogus"}},
        {"name": "ruru_explain", "arguments": {"repo": repo, "id": "none"}},
    ]
    res = rpc(*({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": p} for i, p in enumerate(msgs)))
    survivors = res[1]["result"]["structuredContent"]["survivors"]
    ror = next(s for s in survivors if s["rule"] == "ROR")
    assert res[2]["result"]["structuredContent"]["tamper"]["high"] == 3
    assert res[3]["result"]["isError"] and res[4]["result"]["isError"]
    explain, verify = rpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "ruru_explain", "arguments": {"repo": repo, "id": ror["id"]}},
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "ruru_verify", "arguments": {"repo": repo, "id": ror["id"], "python": py}},
        },
    )
    assert "pct > 100" in explain["result"]["structuredContent"]["context"]
    assert verify["result"]["structuredContent"]["killed"] is False


def test_comment_cap_and_bodies(tmp_path: Path) -> None:
    data = report()
    many = [dict(data["items"][0], id=f"ROR-{n}", line=2) for n in range(8)]
    data["items"] = many
    gh = FakeGitHub()
    path = tmp_path / "r.json"
    path.write_text(json.dumps(data))
    assert github.post(path, gh, PR_EVENT, None, 3)["inline_comments"] == 3
    body = github.comment_body(github.validate(report())["items"][0])
    assert body.startswith("**ruru: survived mutant ROR** `a < b` to `(a <= b)`") and "Tests run:" in body
    ff = {"id": "f", "check": "fail_first", "rule": "passes_without_change", "hint": "h", "after": ""}
    assert github.comment_body(ff).startswith("**ruru: fail first, passes without change**")


def test_validate_bounds() -> None:
    r = report()
    r["items"] = [dict(r["items"][0], line=n, id=f"x{n}") for n in (0, 1, 10**7 - 1, 10**7)]
    r["summary"]["mutants"]["killed"] = 0
    clean = github.validate(r)
    assert [i["line"] for i in clean["items"]] == [1, 10**7 - 1]
    r["summary"]["mutants"]["killed"] = 10**6 - 1
    assert github.validate(r)["summary"]["mutants"]["killed"] == 10**6 - 1


def test_findings_order() -> None:
    items = [
        {"id": "m", "check": "mutant", "file": "b.py", "line": 1, "severity": "high"},
        {"id": "low", "check": "tamper", "file": "a.py", "line": 1, "severity": "medium"},
        {"id": "t", "check": "tamper", "file": "z.py", "line": 1, "severity": "high"},
        {"id": "m2", "check": "mutant", "file": "c.py", "line": 1, "severity": "high"},
        {"id": "m3", "check": "mutant", "file": "c.py", "line": 2, "severity": "high"},
        {"id": "i", "check": "mutant", "file": "a.py", "line": 1, "severity": "info"},
    ]
    assert [i["id"] for i in github.findings({"items": items})] == ["t", "m2", "m3", "m", "low"]


def test_pages_follow_until_short() -> None:
    class Paged(github.GitHub):
        def __init__(self) -> None:
            super().__init__("t", "o/r")
            self.paths: list[str] = []

        def call(self, method: str, path: str, body: Any = None) -> Any:
            self.paths.append(path)
            return [0] * (100 if path.endswith("&page=1") else 7)

    gh = Paged()
    assert len(gh.pages("/x?state=all")) == 107
    assert gh.paths == ["/x?state=all&per_page=100&page=1", "/x?state=all&per_page=100&page=2"]


def test_markdown_table_is_safe() -> None:
    from ruru_review.render import markdown

    clean = github.validate(report())
    clean["items"][0]["before"] = "a | b `x`"
    text = markdown(clean)
    assert "`a \\| b 'x'`" in text and "<b>" not in text and "@\u200bsomeone" in text
