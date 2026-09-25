"""Finds files in a pull request that may duplicate each other.

Jev judges one request at a time, so it cannot notice that two files hold the same logic. Code
finds likely pairs cheaply, and Jev then compares each pair side by side (the `duplication`
check). Only the lines each file adds are compared: that is the new code the pull request brings."""

import posixpath
import re
from collections import Counter, defaultdict
from itertools import combinations

from lisa.models import Line

MAX_PAIRS = 20
# Files adding fewer distinct meaningful lines than this are not compared.
MIN_LINES = 3
# A pair qualifies on shared lines when it shares at least MIN_SHARED of them, covering at least
# MIN_OVERLAP of the smaller file.
MIN_SHARED = 3
MIN_OVERLAP = 0.5
# Shorter lines (closing braces, `else:`) say nothing about duplication.
MIN_LINE_CHARS = 8
# A line added to more files than this is boilerplate, such as a common import.
MAX_FILES_PER_LINE = 10
# A file name shared by more files than this (index.ts, __init__.py) is too common to suggest a copy.
MAX_FILES_PER_NAME = 4
# Of each file's added code, this much is shown to Jev.
MAX_CODE_CHARS = 8_000
# Prose and data files are not code; similar docs are expected, not duplicated logic.
NOT_CODE = re.compile(
    r"\.(md|mdx|markdown|rst|txt|adoc|json|jsonc|ya?ml|toml|ini|cfg|conf|csv|tsv|xml|html?|svg|lock|excalidraw)$",
    re.IGNORECASE,
)


def _meaningful(lines: list[Line]) -> set[str]:
    normalized = {" ".join(line.text.split()) for line in lines}
    return {line for line in normalized if len(line) >= MIN_LINE_CHARS}


def candidate_pairs(added: dict[str, list[Line]]) -> list[tuple[str, str]]:
    """Up to MAX_PAIRS file pairs, most likely duplicates first. A pair qualifies by having the
    same file name in different directories (like two `lib/git.ts`), or by sharing many lines."""
    lines = {
        file: meaningful
        for file, added_lines in added.items()
        if not NOT_CODE.search(file) and len(meaningful := _meaningful(added_lines)) >= MIN_LINES
    }
    scores: Counter[tuple[str, str]] = Counter()

    by_name: dict[str, list[str]] = defaultdict(list)
    for file in lines:
        by_name[posixpath.basename(file)].append(file)
    for files in by_name.values():
        if len(files) <= MAX_FILES_PER_NAME:
            for pair in combinations(sorted(files), 2):
                scores[pair] += 0.5

    files_by_line: dict[str, set[str]] = defaultdict(set)
    for file, meaningful in lines.items():
        for line in meaningful:
            files_by_line[line].add(file)
    shared: Counter[tuple[str, str]] = Counter()
    for files in files_by_line.values():
        if 1 < len(files) <= MAX_FILES_PER_LINE:
            shared.update(combinations(sorted(files), 2))
    for (a, b), count in shared.items():
        overlap = count / min(len(lines[a]), len(lines[b]))
        if count >= MIN_SHARED and overlap >= MIN_OVERLAP:
            scores[(a, b)] += overlap

    ranked = sorted(scores, key=lambda pair: (-scores[pair], pair))
    return ranked[:MAX_PAIRS]


def pair_state(a: str, b: str, added: dict[str, list[Line]]) -> dict:
    """What Jev sees for one pair: each file's name and the code it adds."""

    def code(file: str) -> str:
        text = "\n".join(line.text for line in added[file])
        return text if len(text) <= MAX_CODE_CHARS else text[:MAX_CODE_CHARS] + "\n(truncated)"

    return {"file_a": a, "code_a": code(a), "file_b": b, "code_b": code(b)}
