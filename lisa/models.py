"""Lisa's data structures. Behavior lives in the modules that use them; these hold data and
trivial accessors only."""

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from fnmatch import fnmatchcase

# --- Diffs ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Line:
    kind: str  # '+' added, '-' removed, ' ' context, '@' gap between hunks
    text: str
    number: int | None  # line number in the new file; None for removed lines


@dataclass(frozen=True)
class Chunk:
    """A slice of one file's diff, small enough to ask Jev about in one request."""

    file: str  # empty for the pull request description
    diff: str
    added: tuple[Line, ...]
    start_line: int


# --- Checks --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Kind:
    """One kind of problem a check can find, with what to tell the author about it."""

    label: str
    criteria: str  # what Jev is told this option means
    why: str  # shown to the author: why it matters
    fix: str  # shown to the author: what to do


QUESTION_TYPES = ("noul", "choice", "score")
# What a check is asked about: each chunk of the diff, the pull request as a whole, or pairs of
# files that may duplicate each other (the built-in duplication check only).
SCOPES = ("diff", "pr")


@dataclass(frozen=True)
class Check:
    """A question asked about every chunk of the diff that decides pass or fail, plus up to two
    follow-ups asked in the same request: which kind of problem it is and which added line it is on.

    The deciding question is one of TypeSafe's three types:
    - "noul": yes/no; fails when the probability of yes reaches the threshold.
    - "choice": one of `options`; fails when the combined probability of the `flag` options
      reaches the threshold.
    - "score": a position on `levels` (0 = first level); fails when the score reaches `fail_at`."""

    key: str
    title: str
    instructions: str
    criteria: dict[str, str]  # noul only: optional descriptions of what "true" and "false" mean
    line_instructions: str
    kinds: dict[str, Kind]  # must include "other"
    kind_instructions: str = ""  # without it, no kind follow-up is asked
    note: str = ""  # extra advice added to every comment for this check
    threshold: float | None = None  # noul and choice: overrides the review-wide threshold
    question_type: str = "noul"
    options: dict[str, str] = field(default_factory=dict)  # choice: option -> description
    flag: tuple[str, ...] = ()  # choice: the options that fail the check
    levels: tuple[str, ...] = ()  # score: level descriptions, from best to worst
    fail_at: float | None = None  # score: the score at or above which the check fails
    scope: str = "diff"  # "diff", "pr", or "pairs"

    def matches(self, term: str) -> bool:
        """Whether the term appears in the check's key, title, question, or any of its kinds."""
        term = term.lower()
        texts = [self.key, self.title, self.instructions, *self.kinds]
        texts += [text for kind in self.kinds.values() for text in (kind.label, kind.criteria)]
        return any(term in text.lower() for text in texts)


@dataclass(frozen=True)
class CheckCatalog:
    """An ordered, searchable collection of checks, looked up by key."""

    checks: tuple[Check, ...]

    def __post_init__(self):
        keys = [check.key for check in self.checks]
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        if duplicates:
            raise ValueError(f"Duplicate check keys: {', '.join(duplicates)}")
        missing_other = [check.key for check in self.checks if "other" not in check.kinds]
        if missing_other:
            raise ValueError(f"Checks without an 'other' kind: {', '.join(missing_other)}")

    def __iter__(self) -> Iterator[Check]:
        return iter(self.checks)

    def __len__(self) -> int:
        return len(self.checks)

    def __contains__(self, key: object) -> bool:
        return any(check.key == key for check in self.checks)

    def __getitem__(self, key: str) -> Check:
        check = self.get(key)
        if check is None:
            raise KeyError(f"No check {key!r}; known checks are {', '.join(self.keys())}")
        return check

    def get(self, key: str, default: Check | None = None) -> Check | None:
        return next((check for check in self.checks if check.key == key), default)

    def keys(self) -> list[str]:
        return [check.key for check in self.checks]

    def search(self, term: str) -> list[Check]:
        """Checks whose key, title, question, or kinds mention the term (case-insensitive)."""
        return [check for check in self.checks if check.matches(term)]

    def without(self, keys: Iterable[str]) -> "CheckCatalog":
        excluded = set(keys)
        return CheckCatalog(tuple(check for check in self.checks if check.key not in excluded))

    def scoped(self, scope: str) -> "CheckCatalog":
        return CheckCatalog(tuple(check for check in self.checks if check.scope == scope))

    def extended(self, checks: Iterable[Check]) -> "CheckCatalog":
        return CheckCatalog(self.checks + tuple(checks))


@dataclass(frozen=True)
class Finding:
    check: Check
    kind: Kind
    file: str  # empty for findings in the pull request description
    line: int
    probability: float  # for score questions, TypeSafe's confidence in the score
    text: str = ""  # the flagged line, used to recognize the same finding across pushes
    score: float | None = None  # score questions only
    related: str = ""  # another file involved, such as the other copy of duplicated code

    @property
    def located(self) -> bool:
        """Whether the finding points at an added line, so it can get an inline comment."""
        return bool(self.file and self.text)


@dataclass
class Coverage:
    """How much of the pull request was actually reviewed."""

    files_changed: int = 0
    files_reviewed: int = 0
    chunks: int = 0
    files_unlisted: int = 0  # beyond what GitHub's files API returns
    too_large: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)  # files or "path:line" chunks that could not be reviewed
    skipped: list[str] = field(default_factory=list)  # lockfiles, binaries, vendored, and ignored files

    @property
    def complete(self) -> bool:
        """Whether every file that should have been reviewed was. Deliberately skipped files do not count."""
        return not (self.files_unlisted or self.too_large or self.failed)


@dataclass(frozen=True)
class ReviewResult:
    """Everything a report needs about one review."""

    checks: CheckCatalog
    findings: list[Finding]
    coverage: Coverage
    model: str  # the exact model version that answered
    commit: str  # the head commit that was reviewed
    blob_url: str  # base URL for links to files at that commit
    settings: str  # where the settings came from, for the audit trail

    @property
    def passed(self) -> bool:
        return not self.findings and self.coverage.complete


# --- Configuration -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    """The action's inputs and the GitHub Actions environment."""

    api_key: str
    github_token: str
    model: str
    threshold: float | None  # None when the input is not set
    repo: str
    event_path: str
    api_url: str = "https://api.github.com"
    server_url: str = "https://github.com"
    summary_path: str | None = None
    output_path: str | None = None


@dataclass(frozen=True)
class PullRequest:
    number: int
    head: str
    base: str
    changed_files: int
    title: str = ""
    body: str = ""


@dataclass(frozen=True)
class CustomQuestion:
    """A repository's own question from .lisa.toml, asked about every chunk of the diff.
    See Check for how each question type decides pass or fail."""

    id: str
    question: str
    title: str
    type: str = "noul"
    yes_if: str = ""  # noul
    no_if: str = ""  # noul
    options: dict[str, str] = field(default_factory=dict)  # choice
    flag: tuple[str, ...] = ()  # choice
    levels: tuple[str, ...] = ()  # score
    fail_at: float | None = None  # score
    why: str = "This change matches a rule the repository defines in .lisa.toml."
    fix: str = "Change the code so this rule no longer applies, or discuss the rule with the maintainers."
    threshold: float | None = None  # noul and choice
    scope: str = "diff"  # "diff": each chunk of the diff; "pr": the pull request as a whole


@dataclass(frozen=True)
class RepoConfig:
    """Per-repository settings from .lisa.toml."""

    threshold: float | None = None
    ignore: tuple[str, ...] = ()
    disabled: frozenset[str] = frozenset()
    questions: tuple[CustomQuestion, ...] = ()

    def ignores(self, path: str) -> bool:
        """Whether a path matches an `ignore` pattern. `*` matches across directories, so `docs/*`
        covers everything under docs."""
        return any(fnmatchcase(path, pattern) for pattern in self.ignore)


# --- HTTP ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
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
        """The parsed body. Raises ValueError if it is not JSON."""
        return json.loads(self.body)
