import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

USER_AGENT = "lisa-action"
RETRYABLE = {408, 429, 500, 502, 503, 504, 529}


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self):
        return json.loads(self.body)

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


def request(method: str, url: str, headers: dict[str, str], payload=None, timeout: float = 120) -> Response:
    """Sends one request. HTTP error statuses are returned, not raised; network errors raise OSError."""
    data = None if payload is None else json.dumps(payload).encode()
    all_headers = {"User-Agent": USER_AGENT, **headers}
    if data is not None:
        all_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=all_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return Response(response.status, _lower(response.headers), response.read())
    except urllib.error.HTTPError as error:
        return Response(error.code, _lower(error.headers or {}), error.read())


def send(
    method: str, url: str, headers: dict[str, str], payload=None, attempts: int = 5, base_delay: float = 1.0
) -> Response:
    """Like request, but retries network errors, rate limits, and server errors with
    exponential backoff, honoring retry-after when the server sends it."""
    for attempt in range(1, attempts + 1):
        backoff = base_delay * 2 ** (attempt - 1)
        try:
            response = request(method, url, headers, payload)
        except OSError:
            if attempt == attempts:
                raise
            time.sleep(backoff)
            continue
        # GitHub signals secondary rate limits with a 403 plus retry-after.
        retryable = response.status in RETRYABLE or (response.status == 403 and "retry-after" in response.headers)
        if not retryable or attempt == attempts:
            return response
        try:
            retry_after = float(response.headers.get("retry-after", ""))
        except ValueError:
            retry_after = 0
        time.sleep(retry_after if retry_after > 0 else backoff)
    raise AssertionError("unreachable")


def _lower(headers) -> dict[str, str]:
    return {key.lower(): value for key, value in headers.items()}
