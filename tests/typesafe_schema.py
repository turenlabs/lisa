"""The request rules TypeSafe's /v1/systemone endpoint enforces (docs.typesafe.ai/api), so tests
reject any request the real API would reject. A fake that accepts everything let a request with an
empty instruction and 485 options reach production."""

MAX_CHOICE_OPTIONS = 255
MAX_SCORE_LEVELS = 10


def _text(value) -> bool:
    """Instructions and criteria may be a non-empty string, object, or array."""
    if isinstance(value, str):
        return bool(value.strip())
    return isinstance(value, (dict, list)) and bool(value)


def request_problems(payload: dict) -> list[str]:
    problems = []
    if not isinstance(payload.get("model"), str) or not payload["model"]:
        problems.append("model is required")
    if payload.get("state") in (None, "", {}, []):
        problems.append("state is required")
    questions = payload.get("questions")
    if not isinstance(questions, dict) or not questions:
        return problems + ["questions must be a non-empty map"]
    for key, question in questions.items():
        kind = question.get("type")
        if kind not in ("noul", "choice", "score"):
            problems.append(f"{key}: unknown type {kind!r}")
            continue
        if not _text(question.get("instructions")):
            problems.append(f"{key}: instructions must not be empty")
        criteria = question.get("criteria")
        noul_criteria_ok = criteria is None or (isinstance(criteria, dict) and set(criteria) <= {"true", "false"})
        if kind == "noul" and not noul_criteria_ok:
            problems.append(f"{key}: noul criteria may only describe true and false")
        if kind == "choice" and (not isinstance(criteria, dict) or not 2 <= len(criteria) <= MAX_CHOICE_OPTIONS):
            problems.append(f"{key}: a choice needs 2 to {MAX_CHOICE_OPTIONS} options")
        if kind == "score" and (not isinstance(criteria, list) or not 2 <= len(criteria) <= MAX_SCORE_LEVELS):
            problems.append(f"{key}: a score needs 2 to {MAX_SCORE_LEVELS} levels")
    return problems
