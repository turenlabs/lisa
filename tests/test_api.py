import json

import pytest

from lisa import api
from lisa.errors import ApiError, AuthError
from lisa.models import Response


@pytest.fixture
def respond_with(monkeypatch):
    """Replaces the network with a queue of canned responses (or exceptions to raise)."""
    monkeypatch.setattr(api.time, "sleep", lambda _: None)
    calls = []

    def install(*responses):
        queue = list(responses)

        def fake_send(method, url, headers, payload=None):
            calls.append((method, url, headers, payload))
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            status, body, *headers_ = item
            raw = body if isinstance(body, bytes) else json.dumps(body).encode()
            return Response(status, headers_[0] if headers_ else {}, raw)

        monkeypatch.setattr(api, "send", fake_send)
        return calls

    return install


def client():
    return api.JsonClient("https://x.test", {})


def test_retries_rate_limits_overloads_and_network_errors(respond_with):
    calls = respond_with((429, {}), ConnectionResetError("reset"), (529, {}), (200, {"ok": True}))
    assert client().request("GET", "/").json() == {"ok": True}
    assert len(calls) == 4


def test_raises_after_the_last_attempt(respond_with):
    respond_with(*[(503, {})] * 5)
    with pytest.raises(ApiError, match="503") as error:
        client().request("GET", "/")
    assert error.value.status == 503


def test_network_failure_after_retries_is_an_api_error(respond_with):
    respond_with(*[TimeoutError("timed out")] * 5)
    with pytest.raises(ApiError, match="Could not reach API"):
        client().request("GET", "/")


def test_client_errors_are_not_retried(respond_with):
    calls = respond_with((422, {"detail": "questions.x: bad"}))
    with pytest.raises(ApiError, match=r"422.*questions\.x: bad"):
        client().request("POST", "/")
    assert len(calls) == 1


def test_unauthorized_is_an_auth_error(respond_with):
    respond_with((401, {}))
    with pytest.raises(AuthError):
        client().request("GET", "/")


def test_non_json_body_is_an_api_error(respond_with):
    respond_with((200, b"<html>"))
    with pytest.raises(ApiError, match="Expected JSON"):
        api.parse_json(client().request("GET", "/"))


def test_typesafe_ask_posts_the_request_and_returns_answers(respond_with):
    calls = respond_with((200, {"model": "jev-1.13.0", "answers": {"a": {"noul": 1}}}))
    model, answers = api.TypeSafe("key", "jev-latest").ask({"s": 1}, {"q": 1})
    assert (model, answers) == ("jev-1.13.0", {"a": {"noul": 1}})
    method, url, headers, payload = calls[0]
    assert (method, url) == ("POST", "https://api.typesafe.ai/v1/systemone")
    assert headers["Authorization"] == "Bearer key"
    assert payload == {"model": "jev-latest", "state": {"s": 1}, "questions": {"q": 1}}


def test_typesafe_response_without_answers_is_an_api_error(respond_with):
    respond_with((200, {"detail": "?"}))
    with pytest.raises(ApiError, match="no answers"):
        api.TypeSafe("key", "jev-latest").ask({}, {})


def test_github_retries_secondary_rate_limits(respond_with):
    calls = respond_with((403, {}, {"retry-after": "1"}), (200, []))
    assert api.GitHub("t", "https://gh.test", "o/r").list_files(1) == []
    assert len(calls) == 2


def test_github_paginates(respond_with):
    calls = respond_with((200, [{"n": i} for i in range(100)]), (200, [{"n": 100}]))
    assert len(api.GitHub("t", "https://gh.test", "o/r").list_files(1)) == 101
    assert calls[1][1].endswith("/pulls/1/files?per_page=100&page=2")


def test_github_file_content_returns_none_when_missing(respond_with):
    respond_with((404, {}))
    assert api.GitHub("t", "https://gh.test", "o/r").file_content("a.py", "sha") is None
