import hashlib
from dataclasses import dataclass, field
from urllib.parse import quote

from lisa.checks import CHECKS, Finding

# GitHub rejects comments longer than 65,536 characters.
MAX_COMMENT_CHARS = 60_000

SECRET_HISTORY_NOTE = (
    "Deleting the line in a later commit is not enough: the secret stays in the git history, so rotate it."
)


@dataclass
class Coverage:
    files_reviewed: int = 0
    files_changed: int = 0
    chunks: int = 0
    files_unlisted: int = 0  # beyond what GitHub's files API returns
    too_large: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)  # "path:line" of chunks that could not be reviewed

    @property
    def complete(self) -> bool:
        return not (self.files_unlisted or self.too_large or self.failed)


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def _percent(p: float) -> str:
    return f"{round(p * 100)}%"


def fingerprint(f: Finding) -> str:
    """Identifies a finding by its content rather than its line number, so it survives pushes that shift lines."""
    key = "\0".join([f.check, f.kind, f.file, f.text.strip()])
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def marker(f: Finding) -> str:
    return f"<!-- lisa:{fingerprint(f)} -->"


def render_inline(f: Finding, model: str) -> str:
    details = f.details
    out = [
        marker(f),
        f"**{f.title}: {details.label}** ({_percent(f.probability)} likely)",
        "",
        details.why,
        "",
        f"**How to fix:** {details.fix}",
    ]
    if f.check == "secret":
        out += ["", SECRET_HISTORY_NOTE]
    out += ["", f"<sub>Flagged by Lisa using TypeSafe `{model}`.</sub>"]
    return "\n".join(out)


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
    for key, check in CHECKS.items():
        n = sum(f.check == key for f in findings)
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
                f"- {_plural(coverage.files_unlisted, 'file')} beyond the first 3,000, which is all GitHub's API lists."
            )
        if coverage.too_large:
            names = ", ".join(f"`{name}`" for name in coverage.too_large[:20])
            more = f" and {len(coverage.too_large) - 20:,} more" if len(coverage.too_large) > 20 else ""
            tail.append(f"- {_plural(len(coverage.too_large), 'file')} too large to review: {names}{more}.")
        if coverage.failed:
            spots = ", ".join(f"`{spot}`" for spot in coverage.failed[:20])
            tail.append(
                f"- {_plural(len(coverage.failed), 'diff chunk')} failed after retries: {spots}. Re-run the job to retry."
            )
    tail += ["", "<sub>Inline comments on the diff explain each finding and how to fix it.</sub>" if findings else ""]

    body: list[str] = []
    budget = max_chars - len("\n".join(head + tail)) - 200
    omitted = 0
    for key, check in CHECKS.items():
        group = [f for f in findings if f.check == key]
        if not group:
            continue
        section = ["", f"### {check.title}", ""]
        for f in group:
            location = f"[`{f.file}:{f.line}`]({blob_url}/{quote(f.file)}#L{f.line})"
            item = f"- {location} **{f.details.label}** ({_percent(f.probability)}). {f.details.fix}"
            cost = len(item) + sum(len(line) + 1 for line in section)
            if cost > budget:
                omitted += 1
                continue
            budget -= cost
            body += section
            section = []
            body.append(item)
    if omitted:
        body += ["", f"{_plural(omitted, 'more finding')} not shown here; see the inline comments and the job summary."]

    return "\n".join(head + body + tail).rstrip() + "\n"


def _escape_data(s: str) -> str:
    return s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(s: str) -> str:
    return _escape_data(s).replace(":", "%3A").replace(",", "%2C")


def annotation(f: Finding) -> str:
    """A GitHub workflow command that shows the finding in the job log and on the diff."""
    props = f"file={_escape_property(f.file)},line={f.line},title={_escape_property(f'Lisa: {f.title}')}"
    return f"::error {props}::{_escape_data(f'{f.details.label} ({_percent(f.probability)} likely). {f.details.fix}')}"
