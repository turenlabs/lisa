"""Turns checks into TypeSafe questions, and TypeSafe answers into findings."""

from lisa.default_checks import DEFAULT_CHECKS
from lisa.errors import ConfigError
from lisa.models import Check, CheckCatalog, Chunk, CustomQuestion, Finding, Kind, RepoConfig

DIFF_FORMAT = (
    "Unified diff of one file. Lines starting with '+' were added, lines starting with '-' were removed, "
    "other lines are unchanged context. '@@' separates distant parts of the file. Invisible Unicode characters "
    "are shown as <U+XXXX>."
)
# Choice option text is trimmed; the start of a line is enough to identify it.
MAX_OPTION_CHARS = 200
DEFAULT_THRESHOLD = 0.5
# The pull request description, as a chunk, so description findings flow through the same code.
DESCRIPTION = Chunk(file="", diff="", added=(), start_line=0)


def custom_check(question: CustomQuestion) -> Check:
    """A check for a question from .lisa.yml."""
    criteria = {"true": question.yes_if, "false": question.no_if}
    return Check(
        key=f"custom_{question.id}",
        title=question.title,
        instructions=question.question,
        criteria={answer: text for answer, text in criteria.items() if text},
        line_instructions=f"Which added line in `diff` is most relevant to this question: {question.question}",
        kinds={"other": Kind(question.title, question.question, question.why, question.fix)},
        threshold=question.threshold,
    )


def build_checks(repo_config: RepoConfig) -> CheckCatalog:
    """The built-in checks the repository has not disabled, followed by its custom questions."""
    unknown = repo_config.disabled - set(DEFAULT_CHECKS.keys())
    if unknown:
        raise ConfigError(
            f".lisa.yml disables unknown check(s) {', '.join(sorted(unknown))}; "
            f"built-in checks are {', '.join(DEFAULT_CHECKS.keys())}."
        )
    return DEFAULT_CHECKS.without(repo_config.disabled).extended(custom_check(q) for q in repo_config.questions)


def questions(check: Check, line_options: dict[str, str]) -> dict[str, dict]:
    """The check's yes/no question, plus its kind and line follow-ups when they have a choice to make."""
    gate: dict = {"type": "noul", "instructions": check.instructions}
    if check.criteria:
        gate["criteria"] = check.criteria
    result = {check.key: gate}
    if len(check.kinds) > 1:
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
    """A finding if Jev answered yes (probability at or above the threshold), otherwise None.
    Malformed follow-up answers fall back to the "other" kind and the chunk's first line."""
    probability = _answer(answers, check.key).get("noul")
    if not isinstance(probability, (int, float)) or probability < (check.threshold or threshold):
        return None
    kind = check.kinds.get(_answer(answers, f"{check.key}_kind").get("choice"), check.kinds["other"])
    line = _answer(answers, f"{check.key}_line").get("choice")
    number = int(line) if isinstance(line, str) and line.isdigit() else chunk.start_line
    text = next((added.text for added in chunk.added if added.number == number), "")
    return Finding(check, kind, chunk.file, number, float(probability), text)


def _answer(answers: dict, key: str) -> dict:
    answer = answers.get(key)
    return answer if isinstance(answer, dict) else {}


def state_for(chunk: Chunk) -> dict:
    return {"file": chunk.file, "diff_format": DIFF_FORMAT, "diff": chunk.diff}


def questions_for(chunk: Chunk, checks: CheckCatalog) -> dict[str, dict]:
    """The checks' questions about the chunk, to be asked in a single request."""
    line_options = {str(line.number): line.text.strip()[:MAX_OPTION_CHARS] or "(blank line)" for line in chunk.added}
    return {key: question for check in checks for key, question in questions(check, line_options).items()}


def findings_for(chunk: Chunk, answers: dict, checks: CheckCatalog, threshold: float) -> list[Finding]:
    return [result for check in checks if (result := finding(check, chunk, answers, threshold))]
