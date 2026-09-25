from lisa.duplicates import MAX_PAIRS, candidate_pairs, pair_state
from lisa.models import Line


def lines(*texts: str) -> list[Line]:
    return [Line("+", text, i + 1) for i, text in enumerate(texts)]


GIT = lines(
    "export function git(cwd: string, ...args: string[]) {",
    '  const result = Bun.spawnSync(["git", "-C", cwd, ...args])',
    "  return result.exitCode === 0 ? result.stdout.toString().trim() : undefined",
)


def test_same_file_name_in_different_directories_is_a_candidate():
    other = lines("export function run(cmd: string) {", "  return spawn(cmd, { stdio: 'pipe' })", "  // different")
    pairs = candidate_pairs({"a/lib/git.ts": GIT, "b/lib/git.ts": other, "c/lib/other.ts": other})
    assert ("a/lib/git.ts", "b/lib/git.ts") in pairs


def test_files_sharing_most_of_their_lines_are_candidates():
    pairs = candidate_pairs({"tools/shell.ts": GIT, "scripts/run-git.ts": GIT})
    assert pairs == [("scripts/run-git.ts", "tools/shell.ts")]


def test_unrelated_files_are_not_candidates():
    other = lines("export const answer = computeTheAnswer()", "export const question = unknownQuestion()", "done();")
    assert candidate_pairs({"a.ts": GIT, "b.ts": other}) == []


def test_prose_and_data_files_are_not_compared():
    docs = lines("This page explains the thing.", "It has several lines of text.", "And one more line here.")
    assert candidate_pairs({"a/README.md": docs, "b/README.md": docs, "a/config.yml": docs, "b/config.yml": docs}) == []


def test_tiny_files_and_short_lines_are_not_compared():
    short = lines("}", "else:", "x = 1")
    assert candidate_pairs({"a/util.py": short, "b/util.py": short}) == []


def test_very_common_file_names_are_not_candidates_on_name_alone():
    files = {
        f"pkg{i}/index.ts": lines(
            f"export * from './mod{i}_alpha'", f"export * from './mod{i}_beta'", f"export * from './mod{i}_gamma'"
        )
        for i in range(6)
    }
    assert candidate_pairs(files) == []


def test_boilerplate_lines_shared_by_many_files_do_not_create_pairs():
    boilerplate = 'import { describe, expect, test } from "bun:test"'
    files = {
        f"t{i}.test.ts": lines(boilerplate, f"test('case {i}', () => {{", f"  expect(run({i})).toBe({i})")
        for i in range(12)
    }
    assert candidate_pairs(files) == []


def test_candidates_are_capped():
    files = {f"dir{i}/copy{i}.ts": GIT for i in range(8)}  # 28 identical pairs
    assert len(candidate_pairs(files)) == MAX_PAIRS


def test_a_shared_name_and_shared_lines_outrank_either_alone():
    other = lines("export function unrelated_one() {}", "export function unrelated_two() {}", "const x_value = 3")
    files = {"a/lib/git.ts": GIT, "b/lib/git.ts": GIT, "tools/run.ts": GIT, "c/lib/x.ts": other, "d/lib/x.ts": other}
    pairs = candidate_pairs(files)
    # Same name and same lines first (ties in path order), then same lines alone.
    assert pairs[:2] == [("a/lib/git.ts", "b/lib/git.ts"), ("c/lib/x.ts", "d/lib/x.ts")]
    assert set(pairs[2:]) == {("a/lib/git.ts", "tools/run.ts"), ("b/lib/git.ts", "tools/run.ts")}


def test_pair_state_shows_both_files_and_truncates_long_code():
    long = lines(*[f"line_{i} = compute({i})" for i in range(2_000)])
    state = pair_state("a/git.ts", "b/huge.ts", {"a/git.ts": GIT, "b/huge.ts": long})
    assert state["file_a"] == "a/git.ts" and state["code_a"].startswith("export function git")
    assert state["code_b"].endswith("(truncated)")
