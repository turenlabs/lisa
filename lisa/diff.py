import difflib
import re
from dataclasses import dataclass

# Jev loses accuracy on large states full of unrelated detail, so each request
# sees a small slice of one file's diff.
MAX_CHUNK_CHARS = 12_000
# Added lines are offered as options of a Choice question, which allows at most 255.
MAX_CHUNK_ADDED = 200
# Minified or generated lines can be enormous; the start is enough to judge them.
MAX_LINE_CHARS = 1_000

HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

SKIPPED = [
    re.compile(
        r"(^|/)(package-lock\.json|npm-shrinkwrap\.json|yarn\.lock|pnpm-lock\.yaml|bun\.lockb?|Cargo\.lock"
        r"|poetry\.lock|uv\.lock|Pipfile\.lock|go\.sum|composer\.lock|Gemfile\.lock)$"
    ),
    re.compile(r"\.min\.(js|css)$"),
    re.compile(r"\.(map|snap|svg)$"),
    re.compile(r"(^|/)(node_modules|vendor)/"),
    re.compile(
        r"\.(png|jpe?g|gif|webp|ico|bmp|tiff?|pdf|zip|gz|tgz|bz2|xz|7z|rar|jar|war|class|woff2?|ttf|otf|eot"
        r"|mp[34]|mov|avi|webm|wav|ogg|flac|wasm|so|dylib|dll|exe|bin|o|a|pyc|db|sqlite)$",
        re.IGNORECASE,
    ),
]


@dataclass
class Line:
    kind: str  # '+' added, '-' removed, ' ' context, '@' gap between hunks
    text: str
    number: int | None  # line number in the new file; None for removed lines


@dataclass
class Chunk:
    file: str
    diff: str
    added: list[Line]
    start_line: int


def should_skip(filename: str) -> bool:
    return any(pattern.search(filename) for pattern in SKIPPED)


def unified_patch(old: str, new: str) -> str:
    """Builds a GitHub-style patch (hunks only, no file headers) from two file versions."""
    lines = difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=3)
    return "\n".join(line for i, line in enumerate(lines) if i >= 2)


def parse_patch(patch: str) -> list[list[Line]]:
    """Parses a GitHub file patch into hunks of lines."""
    hunks: list[list[Line]] = []
    hunk: list[Line] | None = None
    number = 0
    for raw in patch.split("\n"):
        header = HUNK_HEADER.match(raw)
        if header:
            number = int(header[1])
            hunk = []
            hunks.append(hunk)
        elif hunk is not None and raw and not raw.startswith("\\"):
            text = raw[1 : MAX_LINE_CHARS + 1]
            if raw[0] == "-":
                hunk.append(Line("-", text, None))
            else:
                hunk.append(Line("+" if raw[0] == "+" else " ", text, number))
                number += 1
    return hunks


def _make_chunk(file: str, lines: list[Line]) -> Chunk:
    added = [line for line in lines if line.kind == "+"]
    numbered = [line.number for line in (added or lines) if line.number is not None]
    diff = "\n".join("@@" if line.kind == "@" else f"{line.kind} {line.text}" for line in lines)
    return Chunk(file=file, diff=diff, added=added, start_line=numbered[0] if numbered else 1)


def chunk_file(
    file: str, patch: str, max_chars: int = MAX_CHUNK_CHARS, max_added: int = MAX_CHUNK_ADDED
) -> list[Chunk]:
    """Splits a file's patch into chunks of at most max_chars and max_added added lines,
    keeping hunks together where they fit. Chunks with no added or removed lines are dropped."""
    chunks: list[Chunk] = []
    current: list[Line] = []
    size = added = 0

    def flush():
        nonlocal current, size, added
        if any(line.kind in "+-" for line in current):
            chunks.append(_make_chunk(file, current))
        current, size, added = [], 0, 0

    for hunk in parse_patch(patch):
        if current:
            current.append(Line("@", "", None))
        for line in hunk:
            length = len(line.text) + 3
            if current and (size + length > max_chars or (line.kind == "+" and added == max_added)):
                flush()
            current.append(line)
            size += length
            added += line.kind == "+"
    flush()
    return chunks
