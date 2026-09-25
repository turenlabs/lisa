"""Renders review results as GitHub-flavored markdown.

File paths come from the pull request author, so they are only ever rendered inside escaped
code spans: a crafted file name must not be able to add links, mentions, or fake headings to
Lisa's comments."""

import hashlib
import re
from urllib.parse import quote

from lisa.models import Finding, ReviewResult

# GitHub rejects comments longer than 65,536 characters.
MAX_COMMENT_CHARS = 60_000
# Lists of skipped files and failed chunks are cut off after this many entries.
MAX_LISTED = 20
CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


def code(text: str) -> str:
    """Untrusted text as a markdown code span that cannot break out of itself."""
    text = CONTROL_CHARACTERS.sub(lambda match: f"\\x{ord(match[0]):02x}", text)
    longest_run = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest_run + 1)
    padding = " " if text.startswith("`") or text.endswith("`") or longest_run else ""
    return f"{fence}{padding}{text}{padding}{fence}"


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def _percent(p: float) -> str:
    return f"{round(p * 100)}%"


def _strength(f: Finding, likely: str = "") -> str:
    """How strongly the finding holds: a probability, or the score for score questions."""
    if f.score is not None:
        return f"score {f.score:.1f} of {len(f.check.levels) - 1}"
    return f"{_percent(f.probability)}{likely}"


def _code_list(items: list[str]) -> str:
    shown = ", ".join(code(item) for item in items[:MAX_LISTED])
    return shown + (f" and {len(items) - MAX_LISTED:,} more" if len(items) > MAX_LISTED else "")


def marker(f: Finding) -> str:
    """An invisible tag identifying a finding by content rather than line number, so re-runs
    recognize findings they already commented on even after the lines moved."""
    key = "\0".join([f.check.key, f.kind.label, f.file, f.text.strip(), f.related])
    return f"<!-- lisa:{hashlib.sha256(key.encode()).hexdigest()[:16]} -->"


def render_inline(f: Finding, model: str) -> str:
    parts = [
        marker(f),
        f"**{f.check.title}: {f.kind.label}** ({_strength(f, ' likely')})",
        f"The same logic is in {code(f.related)}." if f.related else "",
        f.kind.why,
        f"**How to fix:** {f.kind.fix}",
        f.check.note,
        f"<sub>Flagged by Lisa using TypeSafe {code(model)}.</sub>",
    ]
    return "\n\n".join(part for part in parts if part)


def _summary_item(f: Finding, blob_url: str) -> str:
    label = f"**{f.kind.label}** ({_strength(f)})."
    if f.related:
        label += f" Also in {code(f.related)}."
    if not f.file:
        where = "Pull request description:" if f.check.key == "prompt_injection" else "Pull request:"
    else:
        where = f"[{code(f'{f.file}:{f.line}')}]({blob_url}/{quote(f.file)}#L{f.line})"
    # Findings without an inline comment carry their explanation here.
    explanation = f.kind.fix if f.located else f"{f.kind.why} {f.kind.fix}"
    return f"- {where} {label} {explanation}"


def render_summary(comment_marker: str, review: ReviewResult, max_chars: int = MAX_COMMENT_CHARS) -> str:
    coverage = review.coverage
    head = [comment_marker, f"## Lisa review: {'passed' if review.passed else 'changes needed'}", ""]
    head += ["| Check | Result |", "|---|---|"]
    for check in review.checks:
        n = sum(f.check.key == check.key for f in review.findings)
        head.append(f"| {check.title} | {f'**{n} found**' if n else 'Clear'} |")
    head += [
        "",
        f"Reviewed {coverage.files_reviewed:,} of {_plural(coverage.files_changed, 'changed file')} "
        f"in {_plural(coverage.chunks, 'diff chunk')} at commit {code(review.commit[:12])} "
        f"using TypeSafe {code(review.model)}. Settings: {review.settings}.",
    ]

    tail = []
    if not coverage.complete:
        tail += ["", "### Not reviewed", ""]
        if coverage.files_unlisted:
            tail.append(
                f"- {_plural(coverage.files_unlisted, 'file')} beyond the first 3,000, which is all GitHub lists."
            )
        if coverage.too_large:
            tail.append(
                f"- {_plural(len(coverage.too_large), 'file')} too large to review: {_code_list(coverage.too_large)}."
            )
        if coverage.failed:
            tail.append(
                f"- {_plural(len(coverage.failed), 'part')} of the pull request failed after retries: "
                f"{_code_list(coverage.failed)}. Re-run the job to retry."
            )
        tail += ["", "Lisa fails the check until every file has been reviewed."]
    if coverage.skipped:
        tail += [
            "",
            f"<sub>Skipped by design (lockfiles, binaries, generated, vendored, or ignored in .lisa.toml): "
            f"{_code_list(coverage.skipped)}.</sub>",
        ]
    if any(f.located for f in review.findings):
        tail += ["", "<sub>Inline comments on the diff explain each finding and how to fix it.</sub>"]

    # Findings are listed until the comment would exceed GitHub's size limit.
    body: list[str] = []
    budget = max_chars - len("\n".join(head + tail)) - 200
    omitted = 0
    for check in review.checks:
        section = ["", f"### {check.title}", ""]
        for f in (f for f in review.findings if f.check.key == check.key):
            item = _summary_item(f, review.blob_url)
            cost = len(item) + sum(len(line) + 1 for line in section)
            if cost > budget:
                omitted += 1
                continue
            budget -= cost
            body += section + [item]
            section = []
    if omitted:
        body += ["", f"{_plural(omitted, 'more finding')} not shown here; see the inline comments and the job summary."]

    return "\n".join(head + body + tail) + "\n"
