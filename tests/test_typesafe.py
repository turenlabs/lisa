import json

import pytest

from lisa import net, typesafe


@pytest.fixture
def respond_with(monkeypatch):
    monkeypatch.setattr(net.time, "sleep", lambda _: None)
    calls = []

    def install(*responses):
        queue = list(responses)

        def fake_request(method, url, headers, payload=None):
            calls.append((method, url, headers, payload))
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            status, body, *headers_ = item
            return net.Response(status, headers_[0] if headers_ else {}, json.dumps(body).encode())

        monkeypatch.setattr(net, "request", fake_request)
        return calls

    return install


def test_send_retries_rate_limits_overloads_and_network_errors(respond_with):
    calls = respond_with((429, {}), ConnectionResetError("reset"), (529, {}), (200, {"ok": True}))
    assert net.send("GET", "https://x.test", {}).json() == {"ok": True}
    assert len(calls) == 4


def test_send_retries_github_secondary_rate_limits(respond_with):
    calls = respond_with((403, {}, {"retry-after": "1"}), (200, {}))
    assert net.send("GET", "https://x.test", {}).ok
    assert len(calls) == 2


def test_send_returns_the_last_response_when_retries_run_out(respond_with):
    respond_with((529, {}), (529, {}))
    assert net.send("GET", "https://x.test", {}, attempts=2).status == 529


def test_system_one_posts_to_the_endpoint_with_the_key(respond_with):
    calls = respond_with((200, {"answers": {"a": 1}}))
    assert typesafe.system_one("key", {"q": 1}) == {"answers": {"a": 1}}
    method, url, headers, payload = calls[0]
    assert (method, url, payload) == ("POST", "https://api.typesafe.ai/v1/systemone", {"q": 1})
    assert headers["Authorization"] == "Bearer key"


def test_invalid_key_is_an_auth_error_without_retries(respond_with):
    calls = respond_with((401, {}))
    with pytest.raises(typesafe.TypeSafeAuthError):
        typesafe.system_one("key", {})
    assert len(calls) == 1


def test_bad_request_reports_the_detail(respond_with):
    respond_with((422, {"detail": "questions.x: bad"}))
    with pytest.raises(RuntimeError, match=r"422.*questions\.x: bad"):
        typesafe.system_one("key", {})
