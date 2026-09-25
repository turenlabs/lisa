"""Reads and validates the action's inputs, the workflow event, and .lisa.yml."""

import json
import re
from collections.abc import Mapping
from typing import Any

from lisa.errors import ConfigError
from lisa.models import Config, CustomQuestion, PullRequest, RepoConfig

REPO_CONFIG_PATH = ".lisa.yml"
MAX_CUSTOM_QUESTIONS = 20
QUESTION_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
REPO_CONFIG_FIELDS = frozenset({"threshold", "ignore", "checks", "questions"})
QUESTION_FIELDS = frozenset({"id", "question", "title", "yes_if", "no_if", "why", "fix", "threshold"})


def load_config(env: Mapping[str, str]) -> Config:
    def get(name: str, default: str = "") -> str:
        return env.get(name, "").strip() or default

    api_key = get("INPUT_API_KEY")
    if not api_key:
        raise ConfigError(
            "Missing `api-key` input. Add your TypeSafe API key as a repository secret and pass it to Lisa."
        )
    missing = [name for name in ("GITHUB_REPOSITORY", "GITHUB_EVENT_PATH") if not get(name)]
    if missing:
        raise ConfigError(f"Missing {', '.join(missing)}. Lisa must run inside a GitHub Actions workflow.")
    threshold = get("INPUT_THRESHOLD")

    return Config(
        api_key=api_key,
        github_token=get("INPUT_GITHUB_TOKEN"),
        model=get("INPUT_MODEL", "jev-latest"),
        threshold=_threshold(threshold, "The `threshold` input") if threshold else None,
        repo=get("GITHUB_REPOSITORY"),
        event_path=get("GITHUB_EVENT_PATH"),
        api_url=get("GITHUB_API_URL", Config.api_url),
        server_url=get("GITHUB_SERVER_URL", Config.server_url),
        summary_path=get("GITHUB_STEP_SUMMARY") or None,
        output_path=get("GITHUB_OUTPUT") or None,
    )


def load_pull_request(event_path: str) -> PullRequest | None:
    """The pull request in the workflow event, or None if the event is not about one."""
    try:
        with open(event_path, encoding="utf-8") as file:
            event = json.load(file)
    except (OSError, ValueError) as error:
        raise ConfigError(f"Could not read the workflow event at {event_path}: {error}") from error

    pr = event.get("pull_request") if isinstance(event, dict) else None
    if not pr:
        return None
    try:
        return PullRequest(
            number=int(pr["number"]),
            head=pr["head"]["sha"],
            base=pr["base"]["sha"],
            changed_files=int(pr.get("changed_files") or 0),
            title=pr.get("title") or "",
            body=pr.get("body") or "",
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ConfigError(f"The pull request in the workflow event is missing {error}.") from error


def parse_repo_config(text: str) -> RepoConfig:
    """Parses .lisa.yml, raising ConfigError with the exact problem if anything is invalid."""
    try:
        import yaml
    except ImportError as error:
        raise ConfigError(f"Reading {REPO_CONFIG_PATH} needs PyYAML, which could not be imported.") from error
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ConfigError(f"{REPO_CONFIG_PATH} is not valid YAML: {error}") from error

    if data is None:
        return RepoConfig()
    if not isinstance(data, dict):
        raise ConfigError(f"{REPO_CONFIG_PATH} must be a mapping of settings.")
    _reject_unknown(data, REPO_CONFIG_FIELDS, REPO_CONFIG_PATH)

    ignore = data.get("ignore") or []
    if not isinstance(ignore, list) or not all(isinstance(pattern, str) and pattern for pattern in ignore):
        raise ConfigError(f"`ignore` in {REPO_CONFIG_PATH} must be a list of path patterns.")

    checks = data.get("checks") or {}
    if not isinstance(checks, dict) or not all(isinstance(enabled, bool) for enabled in checks.values()):
        raise ConfigError(f"`checks` in {REPO_CONFIG_PATH} must map check names to true or false.")

    raw_questions = data.get("questions") or []
    if not isinstance(raw_questions, list):
        raise ConfigError(f"`questions` in {REPO_CONFIG_PATH} must be a list.")
    if len(raw_questions) > MAX_CUSTOM_QUESTIONS:
        raise ConfigError(f"{REPO_CONFIG_PATH} can define at most {MAX_CUSTOM_QUESTIONS} questions.")
    questions = tuple(_parse_question(item, f"questions[{i}]") for i, item in enumerate(raw_questions))
    ids = [question.id for question in questions]
    duplicates = sorted({question_id for question_id in ids if ids.count(question_id) > 1})
    if duplicates:
        raise ConfigError(f"{REPO_CONFIG_PATH} has duplicate question id(s): {', '.join(duplicates)}.")

    return RepoConfig(
        threshold=_threshold(data["threshold"], f"`threshold` in {REPO_CONFIG_PATH}") if "threshold" in data else None,
        ignore=tuple(ignore),
        disabled=frozenset(str(name) for name, enabled in checks.items() if not enabled),
        questions=questions,
    )


def _parse_question(data: Any, where: str) -> CustomQuestion:
    if not isinstance(data, dict):
        raise ConfigError(f"{where} in {REPO_CONFIG_PATH} must be a mapping with `id` and `question`.")
    _reject_unknown(data, QUESTION_FIELDS, where)

    def text(name: str, required: bool = False) -> str:
        value = data.get(name)
        if value is None and not required:
            return ""
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{where}.{name} in {REPO_CONFIG_PATH} must be a non-empty string.")
        return value.strip()

    question_id = text("id", required=True)
    if not QUESTION_ID.match(question_id):
        raise ConfigError(
            f"{where}.id {question_id!r} must be lowercase letters, digits, '-' or '_' (at most 64 characters)."
        )
    return CustomQuestion(
        id=question_id,
        question=text("question", required=True),
        title=text("title") or question_id,
        yes_if=text("yes_if"),
        no_if=text("no_if"),
        why=text("why") or CustomQuestion.why,
        fix=text("fix") or CustomQuestion.fix,
        threshold=_threshold(data["threshold"], f"{where}.threshold") if "threshold" in data else None,
    )


def _reject_unknown(data: dict, allowed: frozenset[str], where: str):
    unknown = set(map(str, data)) - allowed
    if unknown:
        raise ConfigError(
            f"{where} has unknown setting(s) {', '.join(sorted(unknown))}; allowed: {', '.join(sorted(allowed))}."
        )


def _threshold(value: Any, where: str) -> float:
    try:
        threshold = -1.0 if isinstance(value, bool) else float(value)
    except (TypeError, ValueError):
        threshold = -1.0
    if not 0 < threshold <= 1:
        raise ConfigError(f"{where} must be a number above 0 and at most 1, got {value!r}.")
    return threshold
