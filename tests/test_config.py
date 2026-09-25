import json

import pytest

from lisa.config import Config, PullRequest
from lisa.errors import ConfigError

ENV = {"INPUT_API_KEY": "key", "GITHUB_REPOSITORY": "acme/app", "GITHUB_EVENT_PATH": "/tmp/event.json"}


def test_defaults():
    config = Config.from_env(ENV)
    assert (config.model, config.threshold, config.api_url) == ("jev-latest", 0.5, "https://api.github.com")
    assert config.summary_path is None


def test_blank_inputs_use_defaults():
    config = Config.from_env({**ENV, "INPUT_MODEL": "  ", "INPUT_THRESHOLD": ""})
    assert (config.model, config.threshold) == ("jev-latest", 0.5)


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
        Config.from_env({**ENV, **overrides})


def write(tmp_path, content: str) -> str:
    path = tmp_path / "event.json"
    path.write_text(content)
    return str(path)


def test_reads_the_pull_request_from_the_event(tmp_path):
    event = {"pull_request": {"number": 7, "changed_files": 3, "head": {"sha": "h"}, "base": {"sha": "b"}}}
    assert PullRequest.from_event(write(tmp_path, json.dumps(event))) == PullRequest(7, "h", "b", 3)


def test_events_without_a_pull_request_return_none(tmp_path):
    assert PullRequest.from_event(write(tmp_path, '{"push": {}}')) is None


@pytest.mark.parametrize("content", ["not json", '{"pull_request": {"number": 7}}'])
def test_unreadable_or_incomplete_events_are_config_errors(tmp_path, content):
    with pytest.raises(ConfigError):
        PullRequest.from_event(write(tmp_path, content))


def test_missing_event_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="Could not read"):
        PullRequest.from_event(str(tmp_path / "missing.json"))
