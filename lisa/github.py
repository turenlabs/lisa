from urllib.parse import quote

from lisa import net

# GitHub's pull request files endpoint returns at most 3000 files.
MAX_LISTED_FILES = 3000


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


class GitHub:
    def __init__(self, token: str, api_url: str, repo: str):
        self.base = f"{api_url}/repos/{repo}"
        self.headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _send(self, method: str, path: str, payload=None, accept: str | None = None) -> net.Response:
        headers = {**self.headers, "Accept": accept} if accept else self.headers
        response = net.send(method, f"{self.base}{path}", headers, payload)
        if not response.ok:
            raise GitHubError(f"GitHub {method} {path} failed ({response.status}): {response.text()[:500]}", response.status)
        return response

    def _paginate(self, path: str) -> list:
        items, page = [], 1
        while True:
            batch = self._send("GET", f"{path}?per_page=100&page={page}").json()
            items += batch
            if len(batch) < 100:
                return items
            page += 1

    def list_files(self, pr: int) -> list[dict]:
        return self._paginate(f"/pulls/{pr}/files")

    def file_content(self, path: str, ref: str) -> bytes | None:
        """Raw file contents at a commit, or None if the file does not exist there."""
        try:
            return self._send("GET", f"/contents/{quote(path)}?ref={ref}", accept="application/vnd.github.raw+json").body
        except GitHubError as error:
            if error.status == 404:
                return None
            raise

    def upsert_comment(self, pr: int, marker: str, body: str):
        """Keeps a single comment per PR, identified by an HTML marker, up to date."""
        comments = self._paginate(f"/issues/{pr}/comments")
        existing = next((c for c in comments if marker in (c.get("body") or "")), None)
        if existing:
            return self._send("PATCH", f"/issues/comments/{existing['id']}", {"body": body})
        return self._send("POST", f"/issues/{pr}/comments", {"body": body})

    def review_comment_bodies(self, pr: int) -> list[str]:
        return [c.get("body") or "" for c in self._paginate(f"/pulls/{pr}/comments")]

    def post_review(self, pr: int, commit: str, comments: list[dict]) -> int:
        """Posts inline comments as one review. If GitHub rejects the batch (usually a line
        outside the diff), falls back to posting them one by one and skipping rejects.
        Returns the number of comments posted."""
        try:
            self._send("POST", f"/pulls/{pr}/reviews", {"commit_id": commit, "event": "COMMENT", "comments": comments})
            return len(comments)
        except GitHubError as error:
            if error.status != 422:
                raise
        posted = 0
        for comment in comments:
            try:
                self._send("POST", f"/pulls/{pr}/comments", {"commit_id": commit, **comment})
                posted += 1
            except GitHubError as error:
                if error.status != 422:
                    raise
        return posted
