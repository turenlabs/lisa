"""Clients for the GitHub and TypeSafe HTTP APIs.

Error messages never include response bodies: they can echo request content (the diff, and any
secret in it) into the workflow log. Only the status and a short, known-safe error field are kept."""

import json
import re
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlsplit

from lisa.errors import ApiError, AuthError
from lisa.models import Response

RETRYABLE_STATUSES = {408, 429, 500, 502, 503, 504, 529}
# Upper bound on any response body read into memory.
MAX_RESPONSE_BYTES = 32_000_000
# A server asking for a longer wait is treated as asking for this long.
MAX_RETRY_DELAY = 60.0
MAX_ERROR_DETAIL_CHARS = 120
MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$")


class _SafeRedirects(urllib.request.HTTPRedirectHandler):
    """Follows redirects only over HTTPS, and never forwards credentials to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != "https":
            return None  # the redirect response is returned as an error instead
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and urlsplit(newurl).netloc != urlsplit(req.full_url).netloc:
            redirected.remove_header("Authorization")
        return redirected


_OPENER = urllib.request.build_opener(_SafeRedirects)


def parse_json(response: Response):
    try:
        return response.json()
    except ValueError as error:
        raise ApiError(f"Expected a JSON response (status {response.status}).", response.status) from error


def send(
    method: str, url: str, headers: dict[str, str], payload=None, timeout: float = 120, limit: int = MAX_RESPONSE_BYTES
) -> Response:
    """Sends one request, reading at most `limit` bytes of the body. HTTP error statuses are
    returned, not raised; network errors raise OSError."""
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"User-Agent": "lisa-action", **headers}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return Response(response.status, _lower_keys(response.headers), response.read(limit))
    except urllib.error.HTTPError as error:
        return Response(error.code, _lower_keys(error.headers or {}), error.read(limit))


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

    def request(
        self, method: str, path: str, payload=None, accept: str | None = None, limit: int = MAX_RESPONSE_BYTES
    ) -> Response:
        headers = {**self.headers, "Accept": accept} if accept else self.headers
        for attempt in range(1, self.attempts + 1):
            try:
                response = send(method, self.base_url + path, headers, payload, limit=limit)
            except OSError as error:
                if attempt == self.attempts:
                    raise ApiError(f"Could not reach {self.service}: {type(error).__name__}") from error
                self._wait(attempt)
                continue
            if response.ok:
                return response
            if attempt == self.attempts or not self.should_retry(response):
                error_class = AuthError if response.status == 401 else ApiError
                detail = self.error_detail(response)
                message = f"{self.service} {method} {path.split('?')[0]} failed ({response.status})"
                raise error_class(f"{message}: {detail}" if detail else message, response.status)
            self._wait(attempt, response.headers.get("retry-after"))
        raise AssertionError("unreachable")

    def should_retry(self, response: Response) -> bool:
        return response.status in RETRYABLE_STATUSES

    def error_detail(self, response: Response) -> str:
        """A short description of an error response that cannot contain request content."""
        return ""

    def _wait(self, attempt: int, retry_after: str | None = None):
        try:
            delay = float(retry_after or 0)
        except ValueError:
            delay = 0
        delay = delay if delay > 0 else self.base_delay * 2 ** (attempt - 1)
        time.sleep(min(delay, MAX_RETRY_DELAY))


def _json_field(response: Response, *path: str) -> str:
    try:
        value = response.json()
        for key in path:
            value = value[key]
    except (ValueError, KeyError, TypeError, IndexError):
        return ""
    return value[:MAX_ERROR_DETAIL_CHARS] if isinstance(value, str) else ""


class TypeSafe(JsonClient):
    service = "TypeSafe"

    def __init__(self, api_key: str, model: str):
        super().__init__("https://api.typesafe.ai/v1", {"Authorization": f"Bearer {api_key}"})
        self.model = model

    def error_detail(self, response: Response) -> str:
        # Validation errors can quote the request, so only the error type is kept.
        return _json_field(response, "detail", "error_type")

    def ask(self, state: dict, questions: dict) -> tuple[str, dict]:
        """Asks System One the questions about the state. Returns the model that answered and its answers."""
        payload = {"model": self.model, "state": state, "questions": questions}
        data = parse_json(self.request("POST", "/systemone", payload))
        if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
            raise ApiError("TypeSafe returned a response without answers.")
        model = data.get("model")
        return model if isinstance(model, str) and MODEL_NAME.match(model) else self.model, data["answers"]


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

    def error_detail(self, response: Response) -> str:
        # GitHub's top-level message is a fixed phrase such as "Not Found" or "Validation Failed".
        return _json_field(response, "message")

    def _paginate(self, path: str) -> list[dict]:
        items: list[dict] = []
        page = 1
        while True:
            batch = parse_json(self.request("GET", f"{path}?per_page=100&page={page}"))
            if not isinstance(batch, list):
                raise ApiError(f"GitHub GET {path} returned {type(batch).__name__}, expected a list.")
            items += batch
            if len(batch) < 100:
                return items
            page += 1

    def list_files(self, pr: int) -> list[dict]:
        return self._paginate(f"/pulls/{pr}/files")

    def head_sha(self, pr: int) -> str:
        data = parse_json(self.request("GET", f"/pulls/{pr}"))
        try:
            return data["head"]["sha"]
        except (KeyError, TypeError) as error:
            raise ApiError("GitHub returned a pull request without a head commit.") from error

    def file_content(self, path: str, ref: str, limit: int = MAX_RESPONSE_BYTES) -> bytes | None:
        """Raw file contents at a commit (at most `limit` bytes), or None if the file does not exist there."""
        if any(part in ("", ".", "..") for part in path.split("/")):
            raise ApiError("Refusing to fetch a path with empty, '.' or '..' segments.")
        try:
            url = f"/contents/{quote(path, safe='/')}?ref={quote(ref, safe='')}"
            return self.request("GET", url, accept="application/vnd.github.raw+json", limit=limit).body
        except ApiError as error:
            if error.status == 404:
                return None
            raise

    def _own_comments(self, path: str) -> list[dict]:
        """Comments written by a bot, such as github-actions[bot] or an app. Anyone can write a
        comment containing Lisa's markers, so only bot-authored comments are treated as Lisa's."""
        return [c for c in self._paginate(path) if (c.get("user") or {}).get("type") == "Bot"]

    def upsert_comment(self, pr: int, marker: str, body: str):
        """Keeps a single comment per PR, identified by an HTML marker, up to date."""
        comments = self._own_comments(f"/issues/{pr}/comments")
        existing = next((c for c in comments if marker in (c.get("body") or "")), None)
        if existing:
            self.request("PATCH", f"/issues/comments/{existing['id']}", {"body": body})
        else:
            self.request("POST", f"/issues/{pr}/comments", {"body": body})

    def review_comment_bodies(self, pr: int) -> list[str]:
        return [c.get("body") or "" for c in self._own_comments(f"/pulls/{pr}/comments")]

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
