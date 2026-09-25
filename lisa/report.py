"""Renders review results as GitHub-flavored markdown."""

import hashlib
from urllib.parse import quote

from lisa.models import CheckCatalog, Coverage, Finding

# GitHub rejects comments longer than 65,536 characters.
MAX_COMMENT_CHARS = 60_000
# Lists of skipped files and failed chunks are cut off after this many entries.
MAX_LISTED = 20


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


def _summary_item(f: Finding, blob_url: str) -> str:
    if not f.file:
        # No inline comment is possible on the description, so the explanation goes here.
        return f"- Pull request description: **{f.kind.label}** ({_percent(f.probability)}). {f.kind.why} {f.kind.fix}"
    location = f"[`{f.file}:{f.line}`]({blob_url}/{quote(f.file)}#L{f.line})"
    return f"- {location} **{f.kind.label}** ({_percent(f.probability)}). {f.kind.fix}"


def render_summary(
    comment_marker: str,
    checks: CheckCatalog,
    findings: list[Finding],
    coverage: Coverage,
    model: str,
    blob_url: str,
    max_chars: int = MAX_COMMENT_CHARS,
) -> str:
    passed = not findings and coverage.complete
    head = [comment_marker, f"## Lisa review: {'passed' if passed else 'changes needed'}", ""]
    head += ["| Check | Result |", "|---|---|"]
    for check in checks:
        n = sum(f.check.key == check.key for f in findings)
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
    for check in checks:
        section = ["", f"### {check.title}", ""]
        for f in (f for f in findings if f.check.key == check.key):
            item = _summary_item(f, blob_url)
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
