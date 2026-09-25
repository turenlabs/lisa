import json
from collections.abc import Mapping
from dataclasses import dataclass

from lisa.errors import ConfigError


@dataclass(frozen=True)
class Config:
    api_key: str
    github_token: str
    model: str
    threshold: float
    repo: str
    event_path: str
    api_url: str = "https://api.github.com"
    server_url: str = "https://github.com"
    summary_path: str | None = None
    output_path: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Config":
        def get(name: str, default: str = "") -> str:
            return env.get(name, "").strip() or default

        api_key = get("INPUT_API_KEY")
        if not api_key:
            raise ConfigError(
                "Missing `api-key` input. Add your TypeSafe API key as a repository secret and pass it to Lisa."
            )
        try:
            threshold = float(get("INPUT_THRESHOLD", "0.5"))
        except ValueError:
            threshold = -1
        if not 0 < threshold <= 1:
            raise ConfigError("`threshold` must be a number above 0 and at most 1.")
        missing = [name for name in ("GITHUB_REPOSITORY", "GITHUB_EVENT_PATH") if not get(name)]
        if missing:
            raise ConfigError(f"Missing {', '.join(missing)}. Lisa must run inside a GitHub Actions workflow.")

        return cls(
            api_key=api_key,
            github_token=get("INPUT_GITHUB_TOKEN"),
            model=get("INPUT_MODEL", "jev-latest"),
            threshold=threshold,
            repo=get("GITHUB_REPOSITORY"),
            event_path=get("GITHUB_EVENT_PATH"),
            api_url=get("GITHUB_API_URL", cls.api_url),
            server_url=get("GITHUB_SERVER_URL", cls.server_url),
            summary_path=get("GITHUB_STEP_SUMMARY") or None,
            output_path=get("GITHUB_OUTPUT") or None,
        )


@dataclass(frozen=True)
class PullRequest:
    number: int
    head: str
    base: str
    changed_files: int

    @classmethod
    def from_event(cls, path: str) -> "PullRequest | None":
        """The pull request in the workflow event, or None if the event is not about one."""
        try:
            with open(path, encoding="utf-8") as file:
                event = json.load(file)
        except (OSError, ValueError) as error:
            raise ConfigError(f"Could not read the workflow event at {path}: {error}") from error

        pr = event.get("pull_request") if isinstance(event, dict) else None
        if not pr:
            return None
        try:
            return cls(
                number=int(pr["number"]),
                head=pr["head"]["sha"],
                base=pr["base"]["sha"],
                changed_files=int(pr.get("changed_files") or 0),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ConfigError(f"The pull request in the workflow event is missing {error}.") from error
