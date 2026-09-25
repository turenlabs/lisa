"""Turns checks into TypeSafe questions, and TypeSafe answers into findings.

A missing or invalid answer to a deciding question is an error, never a pass: treating it as
"no problem" would pass code nobody reviewed."""

import math
from collections import defaultdict

from lisa.default_checks import DEFAULT_CHECKS
from lisa.diff import reveal_invisible
from lisa.errors import ApiError, ConfigError
from lisa.models import Check, CheckCatalog, Chunk, CustomQuestion, Finding, Kind, RepoConfig

DIFF_FORMAT = (
    "Unified diff of one file. Lines starting with '+' were added, lines starting with '-' were removed, "
    "other lines are unchanged context. '@@' separates distant parts of the file. Invisible Unicode characters "
    "are shown as <U+XXXX>."
)
# Choice option text is trimmed; the start of a line is enough to identify it.
MAX_OPTION_CHARS = 200
DEFAULT_THRESHOLD = 0.5
# The pull request as a whole, as a chunk, so findings about it flow through the same code.
PULL_REQUEST = Chunk(file="", diff="", added=(), start_line=0)
# How much of the pull request PR-scope questions see: enough to judge its scope, small enough
# for Jev's context.
MAX_PR_DESCRIPTION_CHARS = 8_000
MAX_PR_FILES = 150
MAX_PR_DIRECTORIES = 40


def custom_check(question: CustomQuestion) -> Check:
    """A check for a question from .lisa.toml. Choice and score questions get one kind per flagged
    option or level, so comments name the answer that failed the check."""
    kinds = {"other": Kind(question.title, question.question, question.why, question.fix)}
    if question.type == "choice":
        for name in question.flag:
            kinds[name] = Kind(name, question.options[name], question.why, question.fix)
    if question.type == "score":
        for index, level in enumerate(question.levels):
            kinds[f"level_{index}"] = Kind(level, level, question.why, question.fix)
    criteria = {"true": question.yes_if, "false": question.no_if}
    return Check(
        key=f"custom_{question.id}",
        title=question.title,
        instructions=question.question,
        criteria={answer: text for answer, text in criteria.items() if text},
        line_instructions=f"Which added line in `diff` is most relevant to this question: {question.question}",
        kinds=kinds,
        threshold=question.threshold,
        question_type=question.type,
        options=question.options,
        flag=question.flag,
        levels=question.levels,
        fail_at=question.fail_at,
        scope=question.scope,
    )


def build_checks(repo_config: RepoConfig) -> CheckCatalog:
    """The built-in checks the repository has not disabled, followed by its custom questions."""
    unknown = repo_config.disabled - set(DEFAULT_CHECKS.keys())
    if unknown:
        raise ConfigError(
            f".lisa.toml disables unknown check(s) {', '.join(sorted(unknown))}; "
            f"built-in checks are {', '.join(DEFAULT_CHECKS.keys())}."
        )
    return DEFAULT_CHECKS.without(repo_config.disabled).extended(custom_check(q) for q in repo_config.questions)


def _deciding_question(check: Check) -> dict:
    question: dict = {"type": check.question_type, "instructions": check.instructions}
    if check.question_type == "choice":
        question["criteria"] = check.options
    elif check.question_type == "score":
        question["criteria"] = list(check.levels)
    elif check.criteria:
        question["criteria"] = check.criteria
    return question


def questions(check: Check, line_options: dict[str, str]) -> dict[str, dict]:
    """The check's deciding question, plus its kind and line follow-ups when they have a choice to make."""
    result = {check.key: _deciding_question(check)}
    if check.kind_instructions and len(check.kinds) > 1:
        result[f"{check.key}_kind"] = {
            "type": "choice",
            "instructions": check.kind_instructions,
            "criteria": {name: kind.criteria for name, kind in check.kinds.items()},
        }
    if len(line_options) > 1:
        result[f"{check.key}_line"] = {
            "type": "choice",
            "instructions": check.line_instructions,
            "criteria": line_options,
        }
    return result


def finding(check: Check, chunk: Chunk, answers: dict, threshold: float) -> Finding | None:
    """A finding if the answer fails the check, otherwise None. Malformed follow-up answers fall
    back to the "other" kind and the chunk's first line."""
    answer = _answer(answers, check.key)
    score = None
    if check.question_type == "choice":
        probabilities = _choice_probabilities(answer, check)
        probability = min(1.0, sum(probabilities[name] for name in check.flag))
        if probability < (check.threshold or threshold):
            return None
        kind = check.kinds[max(check.flag, key=lambda name: probabilities[name])]
    elif check.question_type == "score":
        score = _score(answer, check)
        if score < check.fail_at:
            return None
        probability = _unit_number(answer.get("confidence"), 1.0)
        kind = check.kinds[f"level_{min(round(score), len(check.levels) - 1)}"]
    else:
        probability = _noul(answer, check.key)
        if probability < (check.threshold or threshold):
            return None
        kind = check.kinds.get(_answer(answers, f"{check.key}_kind").get("choice"), check.kinds["other"])

    line = _answer(answers, f"{check.key}_line").get("choice")
    number = int(line) if isinstance(line, str) and line.isdigit() else chunk.start_line
    text = next((added.text for added in chunk.added if added.number == number), "")
    return Finding(check, kind, chunk.file, number, probability, text, score)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _unit_number(value, default: float) -> float:
    return float(value) if _is_number(value) and 0 <= value <= 1 else default


def _invalid(key: str) -> ApiError:
    return ApiError(f"TypeSafe returned no valid answer for {key!r}.")


def _noul(answer: dict, key: str) -> float:
    probability = answer.get("noul")
    if not _is_number(probability) or not 0 <= probability <= 1:
        raise _invalid(key)
    return float(probability)


def _choice_probabilities(answer: dict, check: Check) -> dict[str, float]:
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, dict) or not all(
        _is_number(probabilities.get(name)) and 0 <= probabilities[name] <= 1 for name in check.flag
    ):
        raise _invalid(check.key)
    return {name: float(probabilities[name]) for name in check.flag}


def _score(answer: dict, check: Check) -> float:
    score = answer.get("score")
    if not _is_number(score) or not 0 <= score <= len(check.levels) - 1:
        raise _invalid(check.key)
    return float(score)


def _answer(answers: dict, key: str) -> dict:
    answer = answers.get(key)
    return answer if isinstance(answer, dict) else {}


def pr_state(title: str, body: str, files: list[dict]) -> dict:
    """The pull request as a whole, for PR-scope questions: what it says it does, and what it
    changes, by directory and by file (largest changes first)."""

    def churn(f: dict) -> int:
        return int(f.get("additions") or 0) + int(f.get("deletions") or 0)

    directories: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for f in files:
        parts = str(f.get("filename", "")).split("/")[:-1]
        totals = directories["/".join(parts[:3]) or "(root)"]
        totals[0] += 1
        totals[1] += int(f.get("additions") or 0)
        totals[2] += int(f.get("deletions") or 0)
    by_size = sorted(directories.items(), key=lambda item: -(item[1][1] + item[1][2]))
    largest = sorted(files, key=churn, reverse=True)

    description = reveal_invisible(body)
    if len(description) > MAX_PR_DESCRIPTION_CHARS:
        description = description[:MAX_PR_DESCRIPTION_CHARS] + "\n(truncated)"
    return {
        "title": reveal_invisible(title),
        "description": description or "(empty)",
        "summary": f"{len(files)} files changed, "
        f"{sum(int(f.get('additions') or 0) for f in files)} lines added, "
        f"{sum(int(f.get('deletions') or 0) for f in files)} lines removed",
        "directories": [f"{path}: {n} files, +{added} -{removed}" for path, (n, added, removed) in by_size][
            :MAX_PR_DIRECTORIES
        ],
        "files": [
            f"{f.get('status', 'changed')} +{f.get('additions', 0)} -{f.get('deletions', 0)} {f.get('filename', '')}"
            for f in largest[:MAX_PR_FILES]
        ]
        + ([f"(and {len(files) - MAX_PR_FILES} smaller files)"] if len(files) > MAX_PR_FILES else []),
    }


def state_for(chunk: Chunk) -> dict:
    return {"file": chunk.file, "diff_format": DIFF_FORMAT, "diff": chunk.diff}


def questions_for(chunk: Chunk, checks: CheckCatalog) -> dict[str, dict]:
    """The checks' questions about the chunk, to be asked in a single request."""
    line_options = {str(line.number): line.text.strip()[:MAX_OPTION_CHARS] or "(blank line)" for line in chunk.added}
    return {key: question for check in checks for key, question in questions(check, line_options).items()}


def findings_for(chunk: Chunk, answers: dict, checks: CheckCatalog, threshold: float) -> list[Finding]:
    return [result for check in checks if (result := finding(check, chunk, answers, threshold))]
