"""Renders review results as GitHub-flavored markdown."""

import hashlib
from dataclasses import dataclass, field
from urllib.parse import quote

from lisa.checks import CHECKS, Finding

# GitHub rejects comments longer than 65,536 characters.
MAX_COMMENT_CHARS = 60_000
# Lists of skipped files and failed chunks are cut off after this many entries.
MAX_LISTED = 20


@dataclass
class Coverage:
    """How much of the pull request was actually reviewed."""

    files_changed: int = 0
    files_reviewed: int = 0
    chunks: int = 0
    files_unlisted: int = 0  # beyond what GitHub's files API returns
    too_large: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)  # files or "path:line" chunks that could not be reviewed

    @property
    def complete(self) -> bool:
        return not (self.files_unlisted or self.too_large or self.failed)


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def _percent(p: float) -> str:
    return f"{round(p * 100)}%"


def _code_list(items: list[str]) -> str:
    shown = ", ".join(f"`{item}`" for item in items[:MAX_LISTED])
    return shown + (f" and {len(items) - MAX_LISTED:,} more" if len(items) > MAX_LISTED else "")


def marker(f: Finding) -> str:
    """An invisible tag identifying a finding by content rather than line number, so re-runs
    recognize findings they already commented on even after the lines moved."""
    key = "\0".join([f.check.key, f.kind.label, f.file, f.text.strip()])
    return f"<!-- lisa:{hashlib.sha256(key.encode()).hexdigest()[:16]} -->"


def render_inline(f: Finding, model: str) -> str:
    parts = [
        marker(f),
        f"**{f.check.title}: {f.kind.label}** ({_percent(f.probability)} likely)",
        f.kind.why,
        f"**How to fix:** {f.kind.fix}",
        f.check.note,
        f"<sub>Flagged by Lisa using TypeSafe `{model}`.</sub>",
    ]
    return "\n\n".join(part for part in parts if part)


def render_summary(
    comment_marker: str,
    findings: list[Finding],
    coverage: Coverage,
    model: str,
    blob_url: str,
    max_chars: int = MAX_COMMENT_CHARS,
) -> str:
    passed = not findings and coverage.complete
    head = [comment_marker, f"## Lisa review: {'passed' if passed else 'changes needed'}", ""]
    head += ["| Check | Result |", "|---|---|"]
    for check in CHECKS:
        n = sum(f.check is check for f in findings)
        head.append(f"| {check.title} | {f'**{n} found**' if n else 'Clear'} |")
    head += [
        "",
        f"Reviewed {coverage.files_reviewed:,} of {_plural(coverage.files_changed, 'changed file')} "
        f"in {_plural(coverage.chunks, 'diff chunk')} using TypeSafe `{model}`.",
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
                f"- {_plural(len(coverage.failed), 'part')} of the diff failed after retries: "
                f"{_code_list(coverage.failed)}. Re-run the job to retry."
            )
    if findings:
        tail += ["", "<sub>Inline comments on the diff explain each finding and how to fix it.</sub>"]

    # Findings are listed until the comment would exceed GitHub's size limit.
    body: list[str] = []
    budget = max_chars - len("\n".join(head + tail)) - 200
    omitted = 0
    for check in CHECKS:
        section = ["", f"### {check.title}", ""]
        for f in (f for f in findings if f.check is check):
            item = (
                f"- [`{f.file}:{f.line}`]({blob_url}/{quote(f.file)}#L{f.line}) "
                f"**{f.kind.label}** ({_percent(f.probability)}). {f.kind.fix}"
            )
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
