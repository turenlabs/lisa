import json
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor

from lisa.checks import Finding, build_request, interpret
from lisa.diff import Chunk, chunk_file, should_skip, unified_patch
from lisa.github import MAX_LISTED_FILES, GitHub, GitHubError
from lisa.report import Coverage, annotation, marker, render_inline, render_summary
from lisa.typesafe import TypeSafeAuthError, system_one

MARKER = "<!-- lisa-review -->"
CONCURRENCY = 8
# Files whose diff GitHub omits are rebuilt from their contents; beyond this size they are skipped.
MAX_FILE_BYTES = 1_000_000
REVIEW_BATCH = 50
JOB_SUMMARY_CHARS = 900_000


def _append(path: str | None, text: str):
    if path:
        with open(path, "a", encoding="utf-8") as file:
            file.write(text)


def _is_binary(content: bytes) -> bool:
    return b"\0" in content[:8000]


def run(env: Mapping[str, str], log: Callable[[str], None] = print) -> int:
    """Reviews the pull request in the current workflow event. Returns the exit code."""

    def arg(name: str, default: str = "") -> str:
        return env.get(f"INPUT_{name}", "").strip() or default

    api_key = arg("API_KEY")
    if not api_key:
        log("::error::Missing `api-key` input. Add your TypeSafe API key as a repository secret and pass it to Lisa.")
        return 1
    log(f"::add-mask::{api_key}")

    model = arg("MODEL", "jev-latest")
    comment = arg("COMMENT", "true").lower() != "false"
    try:
        threshold = float(arg("THRESHOLD", "0.5"))
    except ValueError:
        threshold = -1
    if not 0 < threshold <= 1:
        raise ValueError("`threshold` must be a number above 0 and at most 1.")

    with open(env["GITHUB_EVENT_PATH"], encoding="utf-8") as file:
        pr = json.load(file).get("pull_request")
    if not pr:
        log("::notice::Lisa reviews pull requests; this event has no pull request, so there is nothing to do.")
        return 0

    number, head, base = pr["number"], pr["head"]["sha"], pr["base"]["sha"]
    repo = env["GITHUB_REPOSITORY"]
    github = GitHub(arg("GITHUB_TOKEN"), env.get("GITHUB_API_URL", "https://api.github.com"), repo)

    listed = github.list_files(number)
    coverage = Coverage(files_changed=max(pr.get("changed_files", 0), len(listed)))
    if len(listed) >= MAX_LISTED_FILES:
        coverage.files_unlisted = coverage.files_changed - len(listed)
    candidates = [f for f in listed if f["status"] != "removed" and not should_skip(f["filename"])]

    def load_patch(f: dict) -> str | None:
        """The file's patch; None if it is too large to review."""
        if f.get("patch"):
            return f["patch"]
        if not f.get("changes"):
            return ""  # renamed or mode change only
        # GitHub leaves out the patch for very large diffs; rebuild it from both versions.
        new = github.file_content(f["filename"], head)
        old = b"" if f["status"] == "added" else github.file_content(f.get("previous_filename", f["filename"]), base)
        new, old = new or b"", old or b""
        if len(new) > MAX_FILE_BYTES or len(old) > MAX_FILE_BYTES:
            return None
        if _is_binary(new) or _is_binary(old):
            return ""
        return unified_patch(old.decode("utf-8", "replace"), new.decode("utf-8", "replace"))

    def load(f: dict) -> tuple[dict, str | None, Exception | None]:
        try:
            return f, load_patch(f), None
        except (GitHubError, OSError) as error:
            return f, "", error

    chunks: list[Chunk] = []
    with ThreadPoolExecutor(CONCURRENCY) as pool:
        for f, patch, error in pool.map(load, candidates):
            if error:
                log(f"::warning::Could not load the diff of {f['filename']}: {error}")
                coverage.failed.append(f["filename"])
            elif patch is None:
                coverage.too_large.append(f["filename"])
            else:
                coverage.files_reviewed += 1
                chunks += chunk_file(f["filename"], patch)
    coverage.chunks = len(chunks)
    log(f"Reviewing {coverage.files_reviewed} files in {len(chunks)} chunks with {model}.")

    def review(chunk: Chunk) -> tuple[str, list[Finding]] | None:
        try:
            response = system_one(api_key, build_request(chunk, model))
            return response.get("model", model), interpret(chunk, response["answers"], threshold)
        except TypeSafeAuthError:
            raise
        except (RuntimeError, ValueError, KeyError, AttributeError) as error:
            log(f"::warning::Could not review {chunk.file}:{chunk.start_line}: {error!r}")
            return None

    findings: list[Finding] = []
    answered_by = model
    with ThreadPoolExecutor(CONCURRENCY) as pool:
        for chunk, result in zip(chunks, pool.map(review, chunks)):
            if result is None:
                coverage.failed.append(f"{chunk.file}:{chunk.start_line}")
            else:
                answered_by, chunk_findings = result
                findings += chunk_findings

    for f in findings:
        log(annotation(f))

    blob_url = f"{env.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}/blob/{head}"
    _append(
        env.get("GITHUB_STEP_SUMMARY"),
        render_summary(MARKER, findings, coverage, answered_by, blob_url, max_chars=JOB_SUMMARY_CHARS),
    )
    _append(env.get("GITHUB_OUTPUT"), f"findings={len(findings)}\n")

    if comment:
        try:
            github.upsert_comment(number, MARKER, render_summary(MARKER, findings, coverage, answered_by, blob_url))
            post_inline_comments(github, number, head, findings, answered_by, log)
        except GitHubError as error:
            # Pull requests from forks get a read-only token; the annotations and job summary still show the results.
            log(f"::warning::Could not post review comments: {error}")

    if findings or coverage.failed:
        problems = [f"{len(findings)} issue(s)"] if findings else []
        if coverage.failed:
            problems.append(f"{len(coverage.failed)} part(s) of the diff that could not be reviewed")
        log(f"::error::Lisa found {' and '.join(problems)}.")
        return 1
    log("Lisa found no issues.")
    return 0


def post_inline_comments(github: GitHub, number: int, head: str, findings: list[Finding], model: str, log) -> None:
    """Comments on each flagged line, skipping findings already commented on by an earlier run."""
    seen = "\n".join(github.review_comment_bodies(number))
    comments = []
    for f in findings:
        if marker(f) in seen:
            continue
        seen += marker(f)
        comments.append({"path": f.file, "line": f.line, "side": "RIGHT", "body": render_inline(f, model)})
    posted = sum(
        github.post_review(number, head, comments[i : i + REVIEW_BATCH]) for i in range(0, len(comments), REVIEW_BATCH)
    )
    log(f"Posted {posted} inline comment(s); {len(findings) - len(comments)} were already on the PR.")
