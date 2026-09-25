import json

import pytest

from lisa.config import load_config, load_pull_request, parse_repo_config
from lisa.errors import ConfigError
from lisa.models import CustomQuestion, PullRequest, RepoConfig

ENV = {"INPUT_API_KEY": "key", "GITHUB_REPOSITORY": "acme/app", "GITHUB_EVENT_PATH": "/tmp/event.json"}


def test_config_defaults():
    config = load_config(ENV)
    assert (config.model, config.threshold, config.api_url) == ("jev-latest", None, "https://api.github.com")
    assert config.summary_path is None


def test_config_reads_the_threshold_input():
    assert load_config({**ENV, "INPUT_THRESHOLD": "0.8"}).threshold == 0.8


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"INPUT_API_KEY": ""}, "Missing `api-key`"),
        ({"INPUT_THRESHOLD": "high"}, "threshold"),
        ({"INPUT_THRESHOLD": "0"}, "threshold"),
        ({"INPUT_THRESHOLD": "1.5"}, "threshold"),
        ({"GITHUB_REPOSITORY": ""}, "GITHUB_REPOSITORY"),
    ],
)
def test_invalid_config_is_rejected(overrides, message):
    with pytest.raises(ConfigError, match=message):
        load_config({**ENV, **overrides})


def write(tmp_path, content: str) -> str:
    path = tmp_path / "event.json"
    path.write_text(content)
    return str(path)


def test_reads_the_pull_request_from_the_event(tmp_path):
    pr = {"number": 7, "changed_files": 3, "head": {"sha": "h"}, "base": {"sha": "b"}, "title": "T", "body": None}
    assert load_pull_request(write(tmp_path, json.dumps({"pull_request": pr}))) == PullRequest(7, "h", "b", 3, "T", "")


def test_events_without_a_pull_request_return_none(tmp_path):
    assert load_pull_request(write(tmp_path, '{"push": {}}')) is None


@pytest.mark.parametrize("content", ["not json", '{"pull_request": {"number": 7}}'])
def test_unreadable_or_incomplete_events_are_config_errors(tmp_path, content):
    with pytest.raises(ConfigError):
        load_pull_request(write(tmp_path, content))


def test_missing_event_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="Could not read"):
        load_pull_request(str(tmp_path / "missing.json"))


FULL = """
threshold: 0.7
ignore:
  - docs/*
checks:
  complexity: false
  secret: true
questions:
  - id: debug-prints
    question: Does this diff add debugging print statements?
    title: Debug output
    yes_if: Adds print or console.log calls used for debugging.
    no_if: Uses the project's logger.
    why: Debug output leaks into production logs.
    fix: Remove it or use the logger.
    threshold: 0.9
  - id: todo
    question: Does this diff add a TODO without a ticket link?
"""


def test_parses_a_full_repo_config():
    config = parse_repo_config(FULL)
    assert config.threshold == 0.7
    assert config.ignore == ("docs/*",)
    assert config.disabled == frozenset({"complexity"})
    debug, todo = config.questions
    assert debug == CustomQuestion(
        id="debug-prints",
        question="Does this diff add debugging print statements?",
        title="Debug output",
        yes_if="Adds print or console.log calls used for debugging.",
        no_if="Uses the project's logger.",
        why="Debug output leaks into production logs.",
        fix="Remove it or use the logger.",
        threshold=0.9,
    )
    assert (todo.title, todo.why, todo.fix, todo.threshold) == ("todo", CustomQuestion.why, CustomQuestion.fix, None)


@pytest.mark.parametrize("text", ["", "# only a comment\n"])
def test_empty_repo_config_uses_defaults(text):
    assert parse_repo_config(text) == RepoConfig()


@pytest.mark.parametrize(
    "text, message",
    [
        ("threshold: [", "not valid YAML"),
        ("- a\n- b", "must be a mapping"),
        ("thresold: 0.5", "unknown setting.*thresold"),
        ("threshold: 2", "threshold"),
        ("threshold: true", "threshold"),
        ("ignore: docs/*", "`ignore`.*list"),
        ("checks:\n  complexity: off-ish", "`checks`.*true or false"),
        ("questions: nope", "`questions`.*list"),
        ("questions:\n  - just a string", r"questions\[0\].*mapping"),
        ("questions:\n  - question: Missing id?", r"questions\[0\]\.id"),
        ("questions:\n  - id: x", r"questions\[0\]\.question"),
        ("questions:\n  - id: Bad Id\n    question: Q?", "lowercase"),
        ("questions:\n  - id: x\n    question: Q?\n    yes: 1", "unknown setting.*yes"),
        ("questions:\n  - id: x\n    question: Q?\n  - id: x\n    question: R?", "duplicate question id.*x"),
    ],
)
def test_invalid_repo_config_is_explained(text, message):
    with pytest.raises(ConfigError, match=message):
        parse_repo_config(text)


def test_repo_config_limits_the_number_of_questions():
    questions = "\n".join(f"  - id: q{i}\n    question: Q{i}?" for i in range(21))
    with pytest.raises(ConfigError, match="at most 20"):
        parse_repo_config(f"questions:\n{questions}")
