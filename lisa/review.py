from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor

from lisa.api import GitHub, TypeSafe
from lisa.checks import Finding, findings_for, questions_for, state_for
from lisa.config import Config, PullRequest
from lisa.diff import Chunk, chunk_file, should_skip, unified_patch
from lisa.errors import ApiError, AuthError, LisaError
from lisa.report import Coverage, marker, render_inline, render_summary

SUMMARY_MARKER = "<!-- lisa-review -->"
CONCURRENCY = 8
# Files whose diff GitHub omits are rebuilt from their contents; beyond this size they are skipped.
MAX_FILE_BYTES = 1_000_000
REVIEW_BATCH = 50
JOB_SUMMARY_CHARS = 900_000

Log = Callable[[str], None]


def run(env: Mapping[str, str], log: Log = print) -> int:
    """Entry point. Returns the process exit code: 1 if the PR has findings or could not be fully reviewed."""
    try:
        config = Config.from_env(env)
        log(f"::add-mask::{config.api_key}")
        pr = PullRequest.from_event(config.event_path)
        if pr is None:
            log("::notice::Lisa reviews pull requests; this event has no pull request, so there is nothing to do.")
            return 0
        return Reviewer(config, pr, log).run()
    except LisaError as error:
        message = str(error).replace("\n", " ")
        log(f"::error::{message}")
        return 1


class Reviewer:
    def __init__(self, config: Config, pr: PullRequest, log: Log = print):
        self.config = config
        self.pr = pr
        self.log = log
        self.github = GitHub(config.github_token, config.api_url, config.repo)
        self.typesafe = TypeSafe(config.api_key, config.model)
        self.coverage = Coverage(files_changed=pr.changed_files)
        self.model = config.model  # replaced by the exact version TypeSafe reports

    def run(self) -> int:
        chunks = self.collect_chunks()
        self.log(f"Reviewing {self.coverage.files_reviewed} files in {len(chunks)} chunks with {self.config.model}.")
        findings = self.review(chunks)
        self.publish(findings)

        if findings or self.coverage.failed:
            problems = [f"{len(findings)} issue(s)"] if findings else []
            if self.coverage.failed:
                problems.append(f"{len(self.coverage.failed)} part(s) of the diff that could not be reviewed")
            self.log(f"::error::Lisa found {' and '.join(problems)}.")
            return 1
        self.log("Lisa found no issues.")
        return 0

    def collect_chunks(self) -> list[Chunk]:
        files = self.github.list_files(self.pr.number)
        self.coverage.files_changed = max(self.coverage.files_changed, len(files))
        if len(files) >= GitHub.MAX_LISTED_FILES:
            self.coverage.files_unlisted = self.coverage.files_changed - len(files)
        candidates = [f for f in files if f.get("status") != "removed" and not should_skip(f.get("filename", ""))]

        chunks: list[Chunk] = []
        with ThreadPoolExecutor(CONCURRENCY) as pool:
            for f, patch in zip(candidates, pool.map(self._load_patch, candidates), strict=True):
                if patch is not None:
                    self.coverage.files_reviewed += 1
                    chunks += chunk_file(f["filename"], patch)
        self.coverage.chunks = len(chunks)
        return chunks

    def _load_patch(self, f: dict) -> str | None:
        """The file's patch, "" if there is nothing to review, or None if it was not reviewed."""
        name = f["filename"]
        if f.get("patch"):
            return f["patch"]
        if not f.get("changes"):
            return ""  # renamed or mode change only
        # GitHub leaves out the patch for very large diffs; rebuild it from both versions.
        try:
            new = self.github.file_content(name, self.pr.head) or b""
            old = b""
            if f.get("status") != "added":
                old = self.github.file_content(f.get("previous_filename", name), self.pr.base) or b""
        except AuthError:
            raise
        except ApiError as error:
            self.log(f"::warning::Could not load the diff of {name}: {error}")
            self.coverage.failed.append(name)
            return None
        if len(new) > MAX_FILE_BYTES or len(old) > MAX_FILE_BYTES:
            self.coverage.too_large.append(name)
            return None
        if b"\0" in new[:8000] or b"\0" in old[:8000]:
            return ""  # binary
        return unified_patch(old.decode("utf-8", "replace"), new.decode("utf-8", "replace"))

    def review(self, chunks: list[Chunk]) -> list[Finding]:
        findings: list[Finding] = []
        with ThreadPoolExecutor(CONCURRENCY) as pool:
            for chunk, result in zip(chunks, pool.map(self._review_chunk, chunks), strict=True):
                if result is None:
                    self.coverage.failed.append(f"{chunk.file}:{chunk.start_line}")
                else:
                    findings += result
        return findings

    def _review_chunk(self, chunk: Chunk) -> list[Finding] | None:
        """The chunk's findings, or None if it could not be reviewed. A rejected key stops the whole review."""
        try:
            self.model, answers = self.typesafe.ask(state_for(chunk), questions_for(chunk))
        except AuthError:
            raise
        except ApiError as error:
            self.log(f"::warning::Could not review {chunk.file}:{chunk.start_line}: {error}")
            return None
        return findings_for(chunk, answers, self.config.threshold)

    def publish(self, findings: list[Finding]):
        blob_url = f"{self.config.server_url}/{self.config.repo}/blob/{self.pr.head}"
        summary = render_summary(SUMMARY_MARKER, findings, self.coverage, self.model, blob_url, JOB_SUMMARY_CHARS)
        _append(self.config.summary_path, summary)
        _append(self.config.output_path, f"findings={len(findings)}\n")
        try:
            comment = render_summary(SUMMARY_MARKER, findings, self.coverage, self.model, blob_url)
            self.github.upsert_comment(self.pr.number, SUMMARY_MARKER, comment)
            self._post_inline_comments(findings)
        except ApiError as error:
            # A read-only token (for example on fork PRs) cannot comment; the job summary still has the results.
            self.log(f"::warning::Could not post review comments: {error}")

    def _post_inline_comments(self, findings: list[Finding]):
        """Comments on each flagged line, skipping findings an earlier run already commented on."""
        seen = set()
        for body in self.github.review_comment_bodies(self.pr.number):
            seen.add(body.split("\n", 1)[0])
        comments = []
        for f in findings:
            if marker(f) not in seen:
                seen.add(marker(f))
                comments.append({"path": f.file, "line": f.line, "side": "RIGHT", "body": render_inline(f, self.model)})
        posted = sum(
            self.github.post_review(self.pr.number, self.pr.head, comments[i : i + REVIEW_BATCH])
            for i in range(0, len(comments), REVIEW_BATCH)
        )
        self.log(f"Posted {posted} inline comment(s); {len(findings) - len(comments)} were already on the PR.")


def _append(path: str | None, text: str):
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as file:
            file.write(text)
    except OSError as error:
        raise LisaError(f"Could not write to {path}: {error}") from error
