# Lisa

A GitHub Action that asks [TypeSafe](https://docs.typesafe.ai/introduction)'s Jev model three questions about every part of a pull request's diff:

- **Does this diff add a secret?**
- **Does this diff introduce a security vulnerability?**
- **Does this diff add unneeded complexity?**

If the answer to any of them is yes, Lisa fails the check. It comments inline on the flagged line, explaining the problem and how to fix it, and keeps one summary comment on the PR up to date.

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
| `threshold` | `0.5` | Probability at which Jev's answer counts as "yes" and fails the PR. Raise it (for example to `0.8`) to flag only clear-cut cases. |
| `comment` | `true` | Post the summary and inline comments. With `false`, results only appear in annotations and the job summary. |
| `model` | `jev-latest` | TypeSafe model. Pin a version such as `jev-1.13.0` to keep results stable. |
| `github-token` | `${{ github.token }}` | Token used to read the diff and write comments. |

Outputs: `findings`, the number of findings.

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
4. Each chunk is one TypeSafe request, and up to 8 run in parallel. For each of the three checks, the request asks:
   - a **Noul**: the yes/no question above, which decides pass or fail;
   - a **Choice** for the kind of problem (SQL injection, hardcoded password, dead code, and so on). The kind selects the explanation and fix shown in the comment;
   - a **Choice** over the chunk's added lines, which picks the line to comment on.
5. Rate limits, overloads, and network errors are retried with backoff. If a chunk still fails, the rest of the review continues, the failure is listed in the summary, and the check fails. A PR is never passed without being fully reviewed.

## Notes

- **Fork PRs:** GitHub does not pass secrets to workflows triggered from forks, so Lisa exits with a "missing `api-key`" error there. Fork PRs also get a read-only token. If commenting fails, Lisa prints a warning and still reports through annotations and the job summary.
- **Data:** diff contents are sent to the TypeSafe API. See TypeSafe's [data handling](https://docs.typesafe.ai/models#data-handling) policy.
- **Runner:** needs `python3` 3.10 or newer, which GitHub-hosted runners include. There are no dependencies to install.
- Jev judges each chunk of the diff on its own, without the rest of the codebase.

## Development

```sh
uv run --with pytest --no-project pytest
```
