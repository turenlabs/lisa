"""Runs a review: load settings, collect the diff, ask TypeSafe, publish the results.

Logging rule: log lines carry counts, check names, file paths, and error summaries only; never
diff content, pull request text, API response bodies, or credentials. Workflow commands are
escaped so text from the pull request (such as a file name) cannot inject commands of its own."""

import os
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor

from lisa.api import GitHub, TypeSafe
from lisa.checks import DEFAULT_THRESHOLD, DESCRIPTION, build_checks, findings_for, questions_for, state_for
from lisa.config import REPO_CONFIG_PATH, load_config, load_pull_request, parse_repo_config
from lisa.default_checks import DESCRIPTION_CHECK
from lisa.diff import chunk_file, reveal_invisible, should_skip, unified_patch
from lisa.errors import ApiError, AuthError, LisaError
from lisa.models import CheckCatalog, Chunk, Config, Coverage, Finding, PullRequest, RepoConfig, ReviewResult
from lisa.report import marker, render_inline, render_summary

SUMMARY_MARKER = "<!-- lisa-review -->"
CONCURRENCY = 8
# Files whose diff GitHub omits are rebuilt from their contents; beyond this size they are not reviewed.
MAX_FILE_BYTES = 1_000_000
# Each check adds up to three questions to a request; batching keeps requests well inside Jev's context.
CHECKS_PER_REQUEST = 4
MAX_DESCRIPTION_CHARS = 20_000
# Caps how much of the TypeSafe budget one pull request can spend. Larger reviews fail closed.
MAX_REQUESTS = 5_000
# Beyond this many inline comments per run, findings are listed only in the summary.
MAX_INLINE_COMMENTS = 100
REVIEW_BATCH = 50
JOB_SUMMARY_CHARS = 900_000

Log = Callable[[str], None]


def command(name: str, message: str = "") -> str:
    """A workflow command (::error::, ::warning::, ...) with its message escaped onto one line."""
    escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::{name}::{escaped}"


def run(env: Mapping[str, str], log: Log = print) -> int:
    """Entry point. Returns the process exit code: 1 if the PR has findings or could not be fully reviewed."""
    try:
        # The API key is never printed, not even in ::add-mask:: (GitHub already masks secrets).
        config = load_config(env)
        pr = load_pull_request(config.event_path)
        if pr is None:
            log(
                command(
                    "notice", "Lisa reviews pull requests; this event has no pull request, so there is nothing to do."
                )
            )
            return 0
        _warn_about_checkout(env, log)
        return Reviewer(config, pr, log).run()
    except LisaError as error:
        log(command("error", str(error)))
        return 1


def _warn_about_checkout(env: Mapping[str, str], log: Log):
    """Lisa never needs the code checked out. With pull_request_target, a checkout of the pull
    request would put untrusted code next to the repository's secrets."""
    workspace = env.get("GITHUB_WORKSPACE", "")
    try:
        checked_out = bool(workspace) and any(os.scandir(workspace))
    except OSError:
        checked_out = False
    if env.get("GITHUB_EVENT_NAME") == "pull_request_target" and checked_out:
        log(
            command(
                "warning",
                "This pull_request_target job has files in its workspace. Lisa does not need a checkout, and "
                "checking out pull request code in a pull_request_target job exposes your secrets to it.",
            )
        )


class Reviewer:
    def __init__(self, config: Config, pr: PullRequest, log: Log = print):
        self.config = config
        self.pr = pr
        self.log = log
        self.github = GitHub(config.github_token, config.api_url, config.repo)
        self.typesafe = TypeSafe(config.api_key, config.model)
        self.coverage = Coverage(files_changed=pr.changed_files)
        self.model = config.model  # replaced by the exact version TypeSafe reports
        self.repo_config = RepoConfig()
        self.checks = build_checks(self.repo_config)
        self.threshold = config.threshold or DEFAULT_THRESHOLD
        self.settings = "defaults"

    def warn(self, message: str):
        self.log(command("warning", message))

    def run(self) -> int:
        self.load_repo_config()
        chunks = self.collect_chunks()
        self.check_budget(chunks)
        self.log(f"Reviewing {self.coverage.files_reviewed} files in {len(chunks)} chunks with {self.config.model}.")
        findings = self.review(chunks) + self.review_description()
        self.publish(findings)

        if findings or not self.coverage.complete:
            problems = [f"{len(findings)} issue(s)"] if findings else []
            if not self.coverage.complete:
                problems.append("parts of the pull request that could not be reviewed")
            self.log(command("error", f"Lisa found {' and '.join(problems)}."))
            return 1
        self.log("Lisa found no issues.")
        return 0

    def load_repo_config(self):
        """Reads .lisa.toml from the base branch, never from the pull request, so a pull request
        cannot switch off the checks that would catch it."""
        content = self.github.file_content(REPO_CONFIG_PATH, self.pr.base)
        if content is None:
            return
        self.repo_config = parse_repo_config(content.decode("utf-8", "replace"))
        self.checks = build_checks(self.repo_config)
        self.threshold = self.config.threshold or self.repo_config.threshold or DEFAULT_THRESHOLD
        self.settings = f"`{REPO_CONFIG_PATH}` from base commit `{self.pr.base[:12]}`"
        self.log(f"Using {REPO_CONFIG_PATH} from the base branch: checks {', '.join(self.checks.keys())}.")

    def collect_chunks(self) -> list[Chunk]:
        files = self.github.list_files(self.pr.number)
        # A push during the review would make the listing describe a different commit.
        if self.github.head_sha(self.pr.number) != self.pr.head:
            raise LisaError(
                "The pull request changed while Lisa was reviewing it; the run for the newer commit reviews it."
            )
        self.coverage.files_changed = max(self.coverage.files_changed, len(files))
        if len(files) >= GitHub.MAX_LISTED_FILES:
            self.coverage.files_unlisted = self.coverage.files_changed - len(files)

        candidates = []
        for f in files:
            name = f.get("filename", "")
            if should_skip(name) or self.repo_config.ignores(name):
                self.coverage.skipped.append(name)
            else:
                candidates.append(f)

        chunks: list[Chunk] = []
        with ThreadPoolExecutor(CONCURRENCY) as pool:
            for f, patch in zip(candidates, pool.map(self._load_patch, candidates), strict=True):
                if patch is not None:
                    self.coverage.files_reviewed += 1
                    chunks += chunk_file(f["filename"], patch)
        self.coverage.chunks = len(chunks)
        return chunks

    def _load_patch(self, f: dict) -> str | None:
        """The file's patch (deletions included), "" if there is nothing to review, or None if it
        could not be reviewed."""
        name = f["filename"]
        if f.get("patch"):
            return f["patch"]
        if not f.get("changes"):
            return ""  # renamed or mode change only
        # GitHub leaves out the patch for very large diffs; rebuild it from both versions.
        try:
            new = b""
            if f.get("status") != "removed":
                new = self.github.file_content(name, self.pr.head, limit=MAX_FILE_BYTES + 1) or b""
            old = b""
            if f.get("status") != "added":
                old_name = f.get("previous_filename", name)
                old = self.github.file_content(old_name, self.pr.base, limit=MAX_FILE_BYTES + 1) or b""
        except AuthError:
            raise
        except ApiError as error:
            self.warn(f"Could not load the diff of {name}: {error}")
            self.coverage.failed.append(name)
            return None
        if len(new) > MAX_FILE_BYTES or len(old) > MAX_FILE_BYTES:
            self.coverage.too_large.append(name)
            return None
        if b"\0" in new[:8000] or b"\0" in old[:8000]:
            self.coverage.skipped.append(name)  # binary
            return ""
        return unified_patch(old.decode("utf-8", "replace"), new.decode("utf-8", "replace"))

    def check_budget(self, chunks: list[Chunk]):
        batches = -(-len(self.checks) // CHECKS_PER_REQUEST)
        needed = len(chunks) * batches + len(self._description_pieces())
        if needed > MAX_REQUESTS:
            raise LisaError(
                f"Reviewing this pull request needs {needed:,} TypeSafe requests, more than the limit of "
                f"{MAX_REQUESTS:,}. Split it into smaller pull requests, or skip generated paths with `ignore` "
                f"in {REPO_CONFIG_PATH}."
            )

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
        findings: list[Finding] = []
        checks = list(self.checks)
        for start in range(0, len(checks), CHECKS_PER_REQUEST):
            batch = CheckCatalog(tuple(checks[start : start + CHECKS_PER_REQUEST]))
            try:
                self.model, answers = self.typesafe.ask(state_for(chunk), questions_for(chunk, batch))
                findings += findings_for(chunk, answers, batch, self.threshold)
            except AuthError:
                raise
            except ApiError as error:
                self.warn(f"Could not review {chunk.file}:{chunk.start_line}: {error}")
                return None
        return findings

    def _description_pieces(self) -> list[str]:
        if "prompt_injection" not in self.checks or not (self.pr.title or self.pr.body):
            return []
        body = reveal_invisible(self.pr.body)
        return [body[i : i + MAX_DESCRIPTION_CHARS] for i in range(0, len(body), MAX_DESCRIPTION_CHARS)] or [""]

    def review_description(self) -> list[Finding]:
        """Checks the whole pull request title and description for prompt injection."""
        check = CheckCatalog((DESCRIPTION_CHECK,))
        for piece in self._description_pieces():
            state = {"title": reveal_invisible(self.pr.title), "description": piece or "(empty)"}
            try:
                self.model, answers = self.typesafe.ask(state, questions_for(DESCRIPTION, check))
                findings = findings_for(DESCRIPTION, answers, check, self.threshold)
            except AuthError:
                raise
            except ApiError as error:
                self.warn(f"Could not review the pull request description: {error}")
                self.coverage.failed.append("pull request description")
                return []
            if findings:
                return findings
        return []

    def publish(self, findings: list[Finding]):
        review = ReviewResult(
            checks=self.checks,
            findings=findings,
            coverage=self.coverage,
            model=self.model,
            commit=self.pr.head,
            blob_url=f"{self.config.server_url}/{self.config.repo}/blob/{self.pr.head}",
            settings=self.settings,
        )
        _append(self.config.summary_path, render_summary(SUMMARY_MARKER, review, max_chars=JOB_SUMMARY_CHARS))
        _append(self.config.output_path, f"findings={len(findings)}\n")
        try:
            self.github.upsert_comment(self.pr.number, SUMMARY_MARKER, render_summary(SUMMARY_MARKER, review))
            self._post_inline_comments([f for f in findings if f.located])
        except ApiError as error:
            # A read-only token (for example on fork PRs) cannot comment; the job summary still has the results.
            self.warn(f"Could not post review comments: {error}")

    def _post_inline_comments(self, findings: list[Finding]):
        """Comments on each flagged line, skipping findings an earlier run already commented on."""
        seen = {body.split("\n", 1)[0] for body in self.github.review_comment_bodies(self.pr.number)}
        comments = []
        for f in findings:
            if marker(f) not in seen:
                seen.add(marker(f))
                comments.append({"path": f.file, "line": f.line, "side": "RIGHT", "body": render_inline(f, self.model)})
        if len(comments) > MAX_INLINE_COMMENTS:
            self.log(f"Posting the first {MAX_INLINE_COMMENTS} of {len(comments)} new inline comments.")
            comments = comments[:MAX_INLINE_COMMENTS]
        posted = sum(
            self.github.post_review(self.pr.number, self.pr.head, comments[i : i + REVIEW_BATCH])
            for i in range(0, len(comments), REVIEW_BATCH)
        )
        self.log(f"Posted {posted} inline comment(s).")


def _append(path: str | None, text: str):
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as file:
            file.write(text)
    except OSError as error:
        raise LisaError(f"Could not write to {path}: {error}") from error
