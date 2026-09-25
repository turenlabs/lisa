<p align="center">
  <img src=".github/lisa-logo.png" alt="Lisa logo: an illustrated cat with the word LISA" width="480">
</p>

**L**eak, **I**njection & **S**implicity **A**uditor.

A GitHub Action that asks [TypeSafe](https://docs.typesafe.ai/introduction)'s Jev model four questions about every part of a pull request's diff:

- **Does this diff add a secret?**
- **Does this diff introduce a security vulnerability?**
- **Does this diff add unneeded complexity?**
- **Does this diff add text that tries to manipulate an AI system?** This covers instructions telling AI reviewers (including Lisa) to approve the change, directives aimed at AI coding agents, and text hidden with invisible Unicode. The PR title and description are checked for this too.

Repositories can add their own questions in a [`.lisa.yml`](#lisayml) file. If the answer to any question is yes, Lisa fails the check. It comments inline on the flagged line, explaining the problem and how to fix it, and keeps one summary comment on the PR up to date.

## Usage

1. Get an API key from the [TypeSafe console](https://console.typesafe.ai/keys).
2. Add it as a repository secret named `TYPESAFE_API_KEY` (**Settings > Secrets and variables > Actions**).
3. Add `.github/workflows/lisa.yml`:

```yaml
name: Lisa

on:
  pull_request:

permissions:
  contents: read
  pull-requests: write

jobs:
  lisa:
    runs-on: ubuntu-latest
    steps:
      - uses: <owner>/lisa@v1
        with:
          api-key: ${{ secrets.TYPESAFE_API_KEY }}
```

That's all. No checkout step is needed: Lisa reads the diff through the GitHub API.

## Inputs

| Input | Default | Description |
|---|---|---|
| `api-key` | | **Required.** Your TypeSafe API key. |
| `threshold` | `threshold` from `.lisa.yml`, else `0.5` | Probability at which Jev's answer counts as "yes" and fails the PR. Raise it (for example to `0.8`) to flag only clear-cut cases. |
| `model` | `jev-latest` | TypeSafe model. Pin a version such as `jev-1.13.0` to keep results stable. |
| `github-token` | `${{ github.token }}` | Token used to read the diff and write comments. |

Outputs: `findings`, the number of findings.

## .lisa.yml

Put a `.lisa.yml` in the root of the repository being reviewed. Every setting is optional.

```yaml
# Probability at which an answer counts as "yes". The action's `threshold` input overrides it.
threshold: 0.6

# Paths to skip. `*` matches across directories, so `docs/*` covers everything under docs.
ignore:
  - docs/*
  - tests/fixtures/*

# Turn built-in checks off: secret, security, complexity, prompt_injection.
checks:
  complexity: false

# Your own yes/no questions, asked about every part of the diff (up to 20).
questions:
  - id: debug-prints                  # required: lowercase letters, digits, - or _
    question: Does this diff add print statements used for debugging?   # required
    title: Debug output               # shown in comments; defaults to the id
    yes_if: Adds print() or console.log() calls that look temporary.   # optional: what counts as yes
    no_if: Output goes through the project's logger.                  # optional: what counts as no
    why: Debug output ends up in production logs.                     # optional: shown to the author
    fix: Remove it or use the logger.                                 # optional: shown to the author
    threshold: 0.8                    # optional: overrides the threshold for this question
```

Write questions as a single, specific yes/no judgment about the diff. Jev is most accurate with narrow questions. Ask two questions rather than one broad one.

**Lisa reads `.lisa.yml` from the PR's base branch, never from the PR itself.** A pull request cannot turn off the checks that would catch it; changes to `.lisa.yml` take effect once merged. An invalid `.lisa.yml` fails the check with a message saying exactly what is wrong.

## What the comments look like

Inline, on the flagged line:

> **Security vulnerability: Injection** (95% likely)
>
> If any part of the interpolated value can be influenced by a user, they can change the meaning of the query or command and read, modify, or delete data, or run commands on the host.
>
> **How to fix:** Use parameterized queries or prepared statements, and pass command arguments as a list instead of a shell string. Never build interpreter input with string formatting.

The summary comment shows a table with each check marked Clear or N found, a linked list of every finding with its fix, and anything that could not be reviewed. Lisa edits the same summary comment on each push, and doesn't repeat an inline comment for a problem it has already flagged, even if the line moved.

## How it works

1. Lisa lists the PR's changed files, all pages up to GitHub's 3,000-file limit. It skips lockfiles, binaries, minified bundles, vendored code, and deleted files.
2. GitHub leaves out the diff for very large files. For those, Lisa downloads both versions and rebuilds the diff itself. Files over 1 MB are listed as not reviewed.
3. Each file's diff is split into chunks of at most about 12,000 characters and 200 added lines. Jev is most accurate on short, focused input.
4. Each chunk is sent to TypeSafe, up to 8 at a time, with at most 4 checks per request so long lists of custom questions stay within Jev's context. Invisible Unicode characters are shown to Jev as visible `<U+XXXX>` markers. For each check, the request asks:
   - a **Noul**: the yes/no question above, which decides pass or fail;
   - a **Choice** for the kind of problem (SQL injection, hardcoded password, dead code, and so on). The kind selects the explanation and fix shown in the comment. Custom questions skip this and use their own `why` and `fix`;
   - a **Choice** over the chunk's added lines, which picks the line to comment on.
5. The PR title and description get one extra request asking the prompt-injection question. Findings there appear in the summary comment, since there is no line to comment on.
6. Rate limits, overloads, and network errors are retried with backoff. If a chunk still fails, the rest of the review continues, the failure is listed in the summary, and the check fails. A PR is never passed without being fully reviewed.

## Notes

- **Fork PRs:** GitHub does not pass secrets to `pull_request` workflows from forks, so Lisa fails with a "missing `api-key`" error there. Public repos that accept fork PRs should trigger on `pull_request_target` instead. That is safe for Lisa because it never checks out or runs PR code, so **do not add a checkout step** to that job. If commenting fails (for example with a read-only token), Lisa prints a warning and the results are still in the job summary.
- **Data:** diff contents are sent to the TypeSafe API. See TypeSafe's [data handling](https://docs.typesafe.ai/models#data-handling) policy.
- **Runner:** needs `python3` 3.10 or newer, which GitHub-hosted runners include. PyYAML is used from the runner if present; otherwise a pinned wheel is installed into a temporary directory, leaving the runner's Python untouched.
- **Adversarial text:** Jev can be swayed by text written to argue with it. The prompt-injection check exists to catch that, but a pass from Lisa is a strong review signal, not a guarantee.
- Jev judges each chunk of the diff on its own, without the rest of the codebase.

## Development

```sh
uv run --with pytest --with pyyaml --no-project pytest
```

The built-in checks live in `lisa/default_checks.py` as a searchable `CheckCatalog`:

```python
from lisa.default_checks import DEFAULT_CHECKS

DEFAULT_CHECKS["secret"]                          # look up by key
[c.key for c in DEFAULT_CHECKS.search("injection")]   # ['security', 'prompt_injection']
```
