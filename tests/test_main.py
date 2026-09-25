import json
from urllib.parse import parse_qs, urlsplit

import pytest

from lisa import net
from lisa.main import run

SQLI = 'db.execute("SELECT * FROM t WHERE id=" + id)'


class FakeApis:
    """Stands in for the GitHub and TypeSafe HTTP APIs."""

    def __init__(self):
        self.calls = []
        self.files = [
            {"filename": "src/db.py", "status": "modified", "changes": 2, "patch": f"@@ -1,1 +1,3 @@\n ok()\n+import os\n+{SQLI}"},
            {"filename": "package-lock.json", "status": "modified", "changes": 2, "patch": "@@ -1 +1 @@\n-a\n+b"},
            {"filename": "old.py", "status": "removed", "changes": 1, "patch": "@@ -1 +0,0 @@\n-gone"},
        ]
        self.contents = {}  # (path, ref) -> bytes
        self.answer = lambda state: {
            "security": {"type": "noul", "noul": 0.95},
            "security_kind": {"type": "choice", "choice": "injection"},
            "security_line": {"type": "choice", "choice": "3"},
            "secret": {"type": "noul", "noul": 0.02},
        }
        self.typesafe_status = 200
        self.issue_comments = [{"id": 99, "body": "unrelated"}]
        self.review_comments = []
        self.review_status = 200

    def request(self, method, url, headers, payload=None):
        self.calls.append({"method": method, "url": url, "headers": headers, "payload": payload})
        path, query = urlsplit(url).path, parse_qs(urlsplit(url).query)
        if url.startswith("https://api.typesafe.ai/"):
            if self.typesafe_status != 200:
                return self._json({"detail": "boom"}, self.typesafe_status)
            return self._json({"model": "jev-1.13.0", "answers": self.answer(payload["state"])})
        if path.endswith("/pulls/7/files"):
            page = int(query["page"][0])
            return self._json(self.files[(page - 1) * 100 : page * 100])
        if "/contents/" in path:
            key = (path.split("/contents/")[1], query["ref"][0])
            return self._raw(self.contents[key]) if key in self.contents else self._json({}, 404)
        if path.endswith("/issues/7/comments") and method == "GET":
            return self._json(self.issue_comments)
        if path.endswith("/issues/7/comments") and method == "POST":
            return self._json({"id": 100}, 201)
        if "/issues/comments/" in path and method == "PATCH":
            return self._json({"id": 42})
        if path.endswith("/pulls/7/comments") and method == "GET":
            return self._json(self.review_comments)
        if path.endswith("/pulls/7/reviews") and method == "POST":
            return self._json({"id": 5}, self.review_status)
        if path.endswith("/pulls/7/comments") and method == "POST":
            return self._json({"id": 6}, 201)
        raise AssertionError(f"unexpected request {method} {url}")

    def _json(self, body, status=200):
        return net.Response(status, {}, json.dumps(body).encode())

    def _raw(self, body):
        return net.Response(200, {}, body)

    def find(self, method, fragment):
        return [c for c in self.calls if c["method"] == method and fragment in c["url"]]


@pytest.fixture
def apis(monkeypatch):
    fake = FakeApis()
    monkeypatch.setattr(net, "request", fake.request)
    monkeypatch.setattr(net.time, "sleep", lambda _: None)
    return fake


@pytest.fixture
def make_env(tmp_path):
    def make(event=None, **inputs):
        pr = {"number": 7, "changed_files": 3, "head": {"sha": "head1"}, "base": {"sha": "base1"}}
        (tmp_path / "event.json").write_text(json.dumps(event or {"pull_request": pr}))
        (tmp_path / "summary.md").write_text("")
        (tmp_path / "output").write_text("")
        env = {
            "INPUT_API_KEY": "ts-key",
            "INPUT_GITHUB_TOKEN": "gh-token",
            "GITHUB_EVENT_PATH": str(tmp_path / "event.json"),
            "GITHUB_REPOSITORY": "acme/app",
            "GITHUB_API_URL": "https://api.github.test",
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
            "GITHUB_OUTPUT": str(tmp_path / "output"),
        }
        env.update({f"INPUT_{k.upper()}": v for k, v in inputs.items()})
        return env

    return make


def test_flags_a_vulnerability_with_summary_inline_comment_and_failing_check(apis, make_env, tmp_path):
    logs = []
    assert run(make_env(), logs.append) == 1

    [typesafe] = apis.find("POST", "api.typesafe.ai")
    assert typesafe["headers"]["Authorization"] == "Bearer ts-key"
    assert typesafe["payload"]["state"]["file"] == "src/db.py", "lockfiles and deleted files are skipped"

    [summary] = apis.find("POST", "/issues/7/comments")
    assert "## Lisa review: changes needed" in summary["payload"]["body"]
    assert "| Security vulnerability | **1 found** |" in summary["payload"]["body"]

    [review] = apis.find("POST", "/pulls/7/reviews")
    assert review["payload"]["commit_id"] == "head1"
    [inline] = review["payload"]["comments"]
    assert (inline["path"], inline["line"], inline["side"]) == ("src/db.py", 3, "RIGHT")
    assert "**Security vulnerability: Injection** (95% likely)" in inline["body"]
    assert "**How to fix:** Use parameterized queries" in inline["body"]

    assert "::add-mask::ts-key" in logs
    assert any(l.startswith("::error file=src/db.py,line=3,") for l in logs)
    assert "findings=1" in (tmp_path / "output").read_text()
    assert "Injection" in (tmp_path / "summary.md").read_text()


def test_reruns_update_the_summary_and_do_not_repeat_inline_comments(apis, make_env):
    assert run(make_env(), lambda _: None) == 1
    [review] = apis.find("POST", "/pulls/7/reviews")
    apis.issue_comments = [{"id": 42, "body": "<!-- lisa-review -->\nold"}]
    apis.review_comments = [{"body": c["body"]} for c in review["payload"]["comments"]]
    apis.calls.clear()

    assert run(make_env(), lambda _: None) == 1
    assert apis.find("PATCH", "/issues/comments/42")
    assert not apis.find("POST", "/issues/7/comments")
    assert not apis.find("POST", "/pulls/7/reviews")


def test_rejected_review_batch_falls_back_to_single_comments(apis, make_env):
    apis.review_status = 422
    assert run(make_env(), lambda _: None) == 1
    [single] = apis.find("POST", "/pulls/7/comments")
    assert single["payload"]["commit_id"] == "head1"


def test_clean_pr_passes(apis, make_env):
    apis.answer = lambda state: {}
    assert run(make_env(), lambda _: None) == 0
    [summary] = apis.find("POST", "/issues/7/comments")
    assert "## Lisa review: passed" in summary["payload"]["body"]
    assert not apis.find("POST", "/pulls/7/reviews")


def test_answers_below_the_threshold_pass(apis, make_env):
    assert run(make_env(threshold="0.99"), lambda _: None) == 0


def test_massive_pr_is_fully_chunked_and_reviewed(apis, make_env):
    added = "\n".join(f"+line_{i} = {i}" for i in range(450))
    apis.files = [
        {"filename": f"src/f{i}.py", "status": "added", "changes": 450, "patch": f"@@ -0,0 +1,450 @@\n{added}"}
        for i in range(250)
    ]
    apis.answer = lambda state: {}
    event = {"pull_request": {"number": 7, "changed_files": 250, "head": {"sha": "h"}, "base": {"sha": "b"}}}
    assert run(make_env(event=event), lambda _: None) == 0
    assert len(apis.find("GET", "/pulls/7/files")) == 3, "all pages are listed"
    assert len(apis.find("POST", "api.typesafe.ai")) == 250 * 3, "450 added lines per file make 3 chunks"


def test_rebuilds_diffs_that_github_omits(apis, make_env):
    apis.files = [{"filename": "src/huge.py", "status": "modified", "changes": 2}]
    apis.contents = {("src/huge.py", "base1"): b"a\nb\n", ("src/huge.py", "head1"): b"a\nB\n" + SQLI.encode() + b"\n"}
    assert run(make_env(), lambda _: None) == 1
    [typesafe] = apis.find("POST", "api.typesafe.ai")
    assert "+ B" in typesafe["payload"]["state"]["diff"]


def test_oversized_and_binary_files_are_not_sent(apis, make_env):
    apis.files = [
        {"filename": "data/huge.sql", "status": "added", "changes": 1},
        {"filename": "data/blob.dat", "status": "added", "changes": 1},
    ]
    apis.contents = {("data/huge.sql", "head1"): b"x" * 2_000_000, ("data/blob.dat", "head1"): b"\0\1\2"}
    assert run(make_env(), lambda _: None) == 0
    assert not apis.find("POST", "api.typesafe.ai")
    [summary] = apis.find("POST", "/issues/7/comments")
    assert "too large to review: `data/huge.sql`" in summary["payload"]["body"]


def test_typesafe_failures_are_reported_and_fail_closed(apis, make_env):
    apis.typesafe_status = 500
    logs = []
    assert run(make_env(), logs.append) == 1
    assert len(apis.find("POST", "api.typesafe.ai")) == 5, "retried before giving up"
    [summary] = apis.find("POST", "/issues/7/comments")
    assert "failed after retries: `src/db.py:2`" in summary["payload"]["body"]


def test_invalid_api_key_aborts(apis, make_env):
    apis.typesafe_status = 401
    with pytest.raises(Exception, match="rejected the API key"):
        run(make_env(), lambda _: None)


def test_a_read_only_token_only_warns(apis, make_env, monkeypatch):
    def forbidden(method, url, headers, payload=None):
        if "/comments" in url:
            return net.Response(403, {}, b"{}")
        return apis.request(method, url, headers, payload)

    monkeypatch.setattr(net, "request", forbidden)
    logs = []
    assert run(make_env(), logs.append) == 1
    assert any(l.startswith("::warning::Could not post review comments") for l in logs)


def test_missing_api_key_fails_with_a_helpful_error(apis, make_env):
    logs = []
    assert run(make_env(api_key=""), logs.append) == 1
    assert "Missing `api-key`" in logs[0]
    assert apis.calls == []


def test_invalid_threshold_is_rejected(apis, make_env):
    with pytest.raises(ValueError, match="threshold"):
        run(make_env(threshold="high"), lambda _: None)


def test_non_pull_request_events_are_skipped(apis, make_env):
    assert run(make_env(event={"push": {}}), lambda _: None) == 0
    assert apis.calls == []
