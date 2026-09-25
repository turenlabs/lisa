"""Clients for the GitHub and TypeSafe HTTP APIs."""

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import quote

from lisa.errors import ApiError, AuthError

RETRYABLE_STATUSES = {408, 429, 500, 502, 503, 504, 529}


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self):
        try:
            return json.loads(self.body)
        except ValueError as error:
            raise ApiError(f"Expected JSON but got: {self.text()[:200]}", self.status) from error


def send(method: str, url: str, headers: dict[str, str], payload=None, timeout: float = 120) -> Response:
    """Sends one request. HTTP error statuses are returned, not raised; network errors raise OSError."""
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"User-Agent": "lisa-action", **headers}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return Response(response.status, _lower_keys(response.headers), response.read())
    except urllib.error.HTTPError as error:
        return Response(error.code, _lower_keys(error.headers or {}), error.read())


def _lower_keys(headers) -> dict[str, str]:
    return {key.lower(): value for key, value in headers.items()}


class JsonClient:
    """A JSON API client that retries network errors, rate limits, and server errors with
    exponential backoff, honoring retry-after, and raises ApiError once retries run out."""

    service = "API"
    attempts = 5
    base_delay = 1.0

    def __init__(self, base_url: str, headers: dict[str, str]):
        self.base_url = base_url
        self.headers = headers

    def request(self, method: str, path: str, payload=None, accept: str | None = None) -> Response:
        headers = {**self.headers, "Accept": accept} if accept else self.headers
        for attempt in range(1, self.attempts + 1):
            try:
                response = send(method, self.base_url + path, headers, payload)
            except OSError as error:
                if attempt == self.attempts:
                    raise ApiError(f"Could not reach {self.service}: {error}") from error
                self._wait(attempt)
                continue
            if response.ok:
                return response
            if attempt == self.attempts or not self.should_retry(response):
                error_class = AuthError if response.status == 401 else ApiError
                message = f"{self.service} {method} {path} failed ({response.status}): {response.text()[:500]}"
                raise error_class(message, response.status)
            self._wait(attempt, response.headers.get("retry-after"))
        raise AssertionError("unreachable")

    def should_retry(self, response: Response) -> bool:
        return response.status in RETRYABLE_STATUSES

    def _wait(self, attempt: int, retry_after: str | None = None):
        try:
            delay = float(retry_after or 0)
        except ValueError:
            delay = 0
        time.sleep(delay if delay > 0 else self.base_delay * 2 ** (attempt - 1))


class TypeSafe(JsonClient):
    service = "TypeSafe"

    def __init__(self, api_key: str, model: str):
        super().__init__("https://api.typesafe.ai/v1", {"Authorization": f"Bearer {api_key}"})
        self.model = model

    def ask(self, state: dict, questions: dict) -> tuple[str, dict]:
        """Asks System One the questions about the state. Returns the model that answered and its answers."""
        data = self.request("POST", "/systemone", {"model": self.model, "state": state, "questions": questions}).json()
        if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
            raise ApiError(f"TypeSafe returned no answers: {str(data)[:200]}")
        return data.get("model") or self.model, data["answers"]


class GitHub(JsonClient):
    service = "GitHub"
    # The pull request files endpoint returns at most this many files.
    MAX_LISTED_FILES = 3000

    def __init__(self, token: str, api_url: str, repo: str):
        super().__init__(
            f"{api_url}/repos/{repo}",
            {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    def should_retry(self, response: Response) -> bool:
        # GitHub signals secondary rate limits with a 403 plus retry-after.
        return super().should_retry(response) or (response.status == 403 and "retry-after" in response.headers)

    def _paginate(self, path: str) -> list[dict]:
        items: list[dict] = []
        page = 1
        while True:
            batch = self.request("GET", f"{path}?per_page=100&page={page}").json()
            if not isinstance(batch, list):
                raise ApiError(f"GitHub GET {path} returned {type(batch).__name__}, expected a list")
            items += batch
            if len(batch) < 100:
                return items
            page += 1

    def list_files(self, pr: int) -> list[dict]:
        return self._paginate(f"/pulls/{pr}/files")

    def file_content(self, path: str, ref: str) -> bytes | None:
        """Raw file contents at a commit, or None if the file does not exist there."""
        try:
            return self.request(
                "GET", f"/contents/{quote(path)}?ref={ref}", accept="application/vnd.github.raw+json"
            ).body
        except ApiError as error:
            if error.status == 404:
                return None
            raise

    def upsert_comment(self, pr: int, marker: str, body: str):
        """Keeps a single comment per PR, identified by an HTML marker, up to date."""
        existing = next((c for c in self._paginate(f"/issues/{pr}/comments") if marker in (c.get("body") or "")), None)
        if existing:
            self.request("PATCH", f"/issues/comments/{existing['id']}", {"body": body})
        else:
            self.request("POST", f"/issues/{pr}/comments", {"body": body})

    def review_comment_bodies(self, pr: int) -> list[str]:
        return [c.get("body") or "" for c in self._paginate(f"/pulls/{pr}/comments")]

    def post_review(self, pr: int, commit: str, comments: list[dict]) -> int:
        """Posts inline comments as one review. If GitHub rejects the batch (usually a line
        outside the diff), posts them one by one and skips the rejects. Returns how many were posted."""
        try:
            self.request(
                "POST", f"/pulls/{pr}/reviews", {"commit_id": commit, "event": "COMMENT", "comments": comments}
            )
            return len(comments)
        except ApiError as error:
            if error.status != 422:
                raise
        posted = 0
        for comment in comments:
            try:
                self.request("POST", f"/pulls/{pr}/comments", {"commit_id": commit, **comment})
                posted += 1
            except ApiError as error:
                if error.status != 422:
                    raise
        return posted
