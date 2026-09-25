import json
import urllib.request

import pytest

from lisa import api
from lisa.errors import ApiError, AuthError
from lisa.models import Response


@pytest.fixture
def respond_with(monkeypatch):
    """Replaces the network with a queue of canned responses (or exceptions to raise)."""
    sleeps = []
    monkeypatch.setattr(api.time, "sleep", sleeps.append)
    calls = []

    def install(*responses):
        queue = list(responses)

        def fake_send(method, url, headers, payload=None, limit=api.MAX_RESPONSE_BYTES):
            calls.append((method, url, headers, payload, limit))
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            status, body, *headers_ = item
            raw = body if isinstance(body, bytes) else json.dumps(body).encode()
            return Response(status, headers_[0] if headers_ else {}, raw)

        monkeypatch.setattr(api, "send", fake_send)
        return calls

    install.sleeps = sleeps
    return install


def client():
    return api.JsonClient("https://x.test", {})


def github():
    return api.GitHub("t", "https://gh.test", "o/r")


def test_retries_rate_limits_overloads_and_network_errors(respond_with):
    calls = respond_with((429, {}), ConnectionResetError("reset"), (529, {}), (200, {"ok": True}))
    assert client().request("GET", "/").json() == {"ok": True}
    assert len(calls) == 4


def test_retry_after_is_honored_but_capped(respond_with):
    respond_with((429, {}, {"retry-after": "5"}), (429, {}, {"retry-after": "999999"}), (200, {}))
    client().request("GET", "/")
    assert respond_with.sleeps == [5.0, api.MAX_RETRY_DELAY]


def test_raises_after_the_last_attempt(respond_with):
    respond_with(*[(503, {})] * 5)
    with pytest.raises(ApiError, match="503") as error:
        client().request("GET", "/")
    assert error.value.status == 503


def test_network_failure_after_retries_names_only_the_error_type(respond_with):
    respond_with(*[TimeoutError("timed out talking to 10.0.0.5")] * 5)
    with pytest.raises(ApiError, match="Could not reach API: TimeoutError") as error:
        client().request("GET", "/")
    assert "10.0.0.5" not in str(error.value)


def test_client_errors_are_not_retried(respond_with):
    calls = respond_with((422, {}))
    with pytest.raises(ApiError, match="422"):
        client().request("POST", "/")
    assert len(calls) == 1


def test_error_messages_never_include_response_bodies(respond_with):
    """TypeSafe validation errors can quote the request, including secrets in the diff."""
    leaked = {"detail": {"error_type": "validation_error", "input": "AWS_SECRET=wJalrXUtnFEMI/K7MDENG"}}
    respond_with((422, leaked))
    with pytest.raises(ApiError) as error:
        api.TypeSafe("key", "jev-latest").ask({}, {})
    assert str(error.value) == "TypeSafe POST /systemone failed (422): validation_error"


def test_github_errors_keep_only_the_top_level_message(respond_with):
    respond_with((404, {"message": "Not Found", "documentation_url": "https://docs"}))
    with pytest.raises(ApiError, match=r"^GitHub GET /pulls/1 failed \(404\): Not Found$"):
        github().head_sha(1)


def test_query_strings_are_left_out_of_error_messages(respond_with):
    respond_with(*[(500, {})] * 5)
    with pytest.raises(ApiError) as error:
        client().request("GET", "/contents/a.py?ref=abc")
    assert "ref=" not in str(error.value)


def test_unauthorized_is_an_auth_error(respond_with):
    respond_with((401, {}))
    with pytest.raises(AuthError):
        client().request("GET", "/")


def test_non_json_body_is_an_api_error_without_the_body(respond_with):
    respond_with((200, b"<html>secret</html>"))
    with pytest.raises(ApiError, match="Expected a JSON response") as error:
        api.parse_json(client().request("GET", "/"))
    assert "secret" not in str(error.value)


def test_typesafe_ask_posts_the_request_and_returns_answers(respond_with):
    calls = respond_with((200, {"model": "jev-1.13.0", "answers": {"a": {"noul": 1}}}))
    model, answers = api.TypeSafe("key", "jev-latest").ask({"s": 1}, {"q": 1})
    assert (model, answers) == ("jev-1.13.0", {"a": {"noul": 1}})
    method, url, headers, payload, _ = calls[0]
    assert (method, url) == ("POST", "https://api.typesafe.ai/v1/systemone")
    assert headers["Authorization"] == "Bearer key"
    assert payload == {"model": "jev-latest", "state": {"s": 1}, "questions": {"q": 1}}


@pytest.mark.parametrize("reported", ["jev`](https://evil) **pass**", "x" * 200, 42, None])
def test_an_unexpected_model_name_falls_back_to_the_requested_one(respond_with, reported):
    respond_with((200, {"model": reported, "answers": {}}))
    assert api.TypeSafe("key", "jev-latest").ask({}, {})[0] == "jev-latest"


def test_typesafe_response_without_answers_is_an_api_error(respond_with):
    respond_with((200, {"detail": "?"}))
    with pytest.raises(ApiError, match="without answers"):
        api.TypeSafe("key", "jev-latest").ask({}, {})


def test_github_retries_secondary_rate_limits(respond_with):
    calls = respond_with((403, {}, {"retry-after": "1"}), (200, []))
    assert github().list_files(1) == []
    assert len(calls) == 2


def test_github_paginates(respond_with):
    calls = respond_with((200, [{"n": i} for i in range(100)]), (200, [{"n": 100}]))
    assert len(github().list_files(1)) == 101
    assert calls[1][1].endswith("/pulls/1/files?per_page=100&page=2")


def test_github_file_content_returns_none_when_missing_and_passes_the_size_limit(respond_with):
    calls = respond_with((404, {}))
    assert github().file_content("src/a b.py", "abc", limit=10) is None
    assert calls[0][1] == "https://gh.test/repos/o/r/contents/src/a%20b.py?ref=abc"
    assert calls[0][4] == 10


@pytest.mark.parametrize("path", ["../secrets", "a/../../b", "a//b", "./a", ""])
def test_github_file_content_refuses_unusual_paths(respond_with, path):
    calls = respond_with()
    with pytest.raises(ApiError, match="Refusing"):
        github().file_content(path, "abc")
    assert calls == []


def test_only_bot_comments_count_as_lisas(respond_with):
    comments = [
        {"id": 1, "user": {"type": "User"}, "body": "<!-- lisa-review --> fake passed"},
        {"id": 2, "user": {"type": "Bot"}, "body": "<!-- lisa-review --> real"},
    ]
    calls = respond_with((200, comments), (200, {}))
    github().upsert_comment(7, "<!-- lisa-review -->", "new")
    assert calls[1][:2] == ("PATCH", "https://gh.test/repos/o/r/issues/comments/2")


def test_a_users_marker_comment_does_not_stop_lisa_from_commenting(respond_with):
    calls = respond_with((200, [{"id": 1, "user": {"type": "User"}, "body": "<!-- lisa-review -->"}]), (201, {}))
    github().upsert_comment(7, "<!-- lisa-review -->", "new")
    assert calls[1][:2] == ("POST", "https://gh.test/repos/o/r/issues/7/comments")


def test_review_comment_bodies_ignore_non_bot_authors(respond_with):
    respond_with(
        (200, [{"user": {"type": "User"}, "body": "a"}, {"user": {"type": "Bot"}, "body": "b"}, {"body": "c"}])
    )
    assert github().review_comment_bodies(7) == ["b"]


def _redirect(original_url: str, new_url: str):
    handler = api._SafeRedirects()
    request = urllib.request.Request(original_url, headers={"Authorization": "Bearer t"})
    return handler.redirect_request(request, None, 302, "Found", {}, new_url)


def test_redirects_to_another_host_drop_credentials():
    redirected = _redirect("https://api.github.com/x", "https://objects.example.com/y")
    assert redirected is not None and not redirected.has_header("Authorization")


def test_redirects_to_the_same_host_keep_credentials():
    assert _redirect("https://api.github.com/x", "https://api.github.com/y").has_header("Authorization")


def test_redirects_away_from_https_are_refused():
    assert _redirect("https://api.github.com/x", "http://api.github.com/y") is None
