from dataclasses import replace

import pytest

from lisa.checks import PULL_REQUEST, build_checks, custom_check, findings_for, questions_for, state_for
from lisa.default_checks import DEFAULT_CHECKS, DESCRIPTION_CHECK
from lisa.errors import ApiError, ConfigError
from lisa.models import CheckCatalog, Chunk, CustomQuestion, Line, RepoConfig

CHUNK = Chunk(
    file="src/db.py",
    diff='+ import os\n+ db.execute("SELECT * FROM users WHERE id = " + id)',
    added=(Line("+", "import os", 6), Line("+", 'db.execute("SELECT * FROM users WHERE id = " + id)', 7)),
    start_line=6,
)
NO = {check.key: {"type": "noul", "noul": 0.0} for check in DEFAULT_CHECKS}
DEBUG = CustomQuestion(
    id="debug-prints", question="Does this diff add debugging print statements?", title="Debug output", fix="Remove it."
)


def test_state_holds_the_file_and_its_diff():
    state = state_for(CHUNK)
    assert state["file"] == "src/db.py"
    assert state["diff"] == CHUNK.diff


def test_each_default_check_asks_a_gate_kind_and_line_question():
    questions = questions_for(CHUNK, DEFAULT_CHECKS.scoped("diff"))
    for check in DEFAULT_CHECKS.scoped("diff"):
        gate = questions[check.key]
        assert gate["type"] == "noul"
        assert gate["instructions"] == check.instructions
        assert set(gate["criteria"]) == {"true", "false"}
        assert set(questions[f"{check.key}_kind"]["criteria"]) == set(check.kinds)
        assert questions[f"{check.key}_line"]["criteria"] == {
            "6": "import os",
            "7": 'db.execute("SELECT * FROM users WHERE id = " + id)',
        }


def test_line_question_is_skipped_when_there_is_one_candidate_line():
    assert "security_line" not in questions_for(replace(CHUNK, added=CHUNK.added[:1]), DEFAULT_CHECKS)


def test_custom_questions_ask_the_gate_and_line_but_no_kind():
    check = custom_check(DEBUG)
    questions = questions_for(CHUNK, CheckCatalog((check,)))
    assert set(questions) == {"custom_debug-prints", "custom_debug-prints_line"}
    assert questions["custom_debug-prints"] == {"type": "noul", "instructions": DEBUG.question}


def test_custom_question_criteria_are_included_when_given():
    check = custom_check(replace(DEBUG, yes_if="print() calls", no_if="logging calls"))
    assert check.criteria == {"true": "print() calls", "false": "logging calls"}


def test_build_checks_disables_and_adds_checks():
    checks = build_checks(RepoConfig(disabled=frozenset({"complexity"}), questions=(DEBUG,)))
    assert checks.keys() == ["secret", "security", "prompt_injection", "duplication", "custom_debug-prints"]


def test_build_checks_rejects_unknown_check_names():
    with pytest.raises(ConfigError, match="unknown check.*complexty"):
        build_checks(RepoConfig(disabled=frozenset({"complexty"})))


def test_yes_answers_become_findings_with_kind_and_line():
    answers = {
        **NO,
        "secret": {"type": "noul", "noul": 0.1},
        "security": {"type": "noul", "noul": 0.97},
        "security_kind": {"type": "choice", "choice": "injection"},
        "security_line": {"type": "choice", "choice": "7"},
        "complexity": {"type": "noul", "noul": 0.5},
    }
    findings = findings_for(CHUNK, answers, DEFAULT_CHECKS, threshold=0.5)
    assert [(f.check.key, f.kind.label, f.line, f.probability) for f in findings] == [
        ("security", "Injection", 7, 0.97),
        ("complexity", "Unneeded complexity", 6, 0.5),
    ]
    assert findings[0].text.startswith("db.execute")


def test_threshold_controls_what_counts_as_yes():
    answers = {**NO, "security": {"type": "noul", "noul": 0.7}}
    assert findings_for(CHUNK, answers, DEFAULT_CHECKS, threshold=0.8) == []
    assert len(findings_for(CHUNK, answers, DEFAULT_CHECKS, threshold=0.6)) == 1


def test_a_check_threshold_overrides_the_review_threshold():
    strict = CheckCatalog((custom_check(replace(DEBUG, threshold=0.9)),))
    answers = {"custom_debug-prints": {"noul": 0.8}}  # below this question's own threshold
    assert findings_for(CHUNK, answers, strict, threshold=0.5) == []


def test_custom_findings_use_the_questions_explanation():
    checks = CheckCatalog((custom_check(DEBUG),))
    [finding] = findings_for(CHUNK, {"custom_debug-prints": {"noul": 0.9}}, checks, threshold=0.5)
    assert (finding.check.title, finding.kind.label, finding.kind.fix) == ("Debug output", "Debug output", "Remove it.")


def test_malformed_follow_up_answers_fall_back_instead_of_crashing():
    answers = {**NO, "security": {"noul": 0.9}, "security_kind": {"choice": "not-a-kind"}, "security_line": "garbage"}
    [finding] = findings_for(CHUNK, answers, DEFAULT_CHECKS, threshold=0.5)
    assert (finding.check.key, finding.kind.label, finding.line) == ("security", "Security vulnerability", 6)


@pytest.mark.parametrize("gate", [None, "yes", {"noul": "yes"}, {"noul": True}, {"noul": 1.5}, {"noul": float("nan")}])
def test_a_missing_or_invalid_yes_no_answer_is_an_error_not_a_pass(gate):
    answers = {**NO, "security": gate}
    with pytest.raises(ApiError, match="no valid answer for 'security'"):
        findings_for(CHUNK, answers, DEFAULT_CHECKS, threshold=0.5)


def test_description_check_is_the_prompt_injection_check_worded_for_the_description():
    assert DESCRIPTION_CHECK.key == "prompt_injection"
    assert "`description`" in DESCRIPTION_CHECK.instructions
    assert set(questions_for(PULL_REQUEST, CheckCatalog((DESCRIPTION_CHECK,)))) == {
        "prompt_injection",
        "prompt_injection_kind",
    }


LICENSE = CustomQuestion(
    id="license",
    question="Which license does the added code come under?",
    title="License",
    type="choice",
    options={"none": "No third-party code.", "mit": "MIT.", "gpl": "GPL.", "unknown": "No clear license."},
    flag=("gpl", "unknown"),
)
COVERAGE = CustomQuestion(
    id="tests",
    question="How well do tests cover the added code?",
    title="Test coverage",
    type="score",
    levels=("Fully tested", "Partly tested", "Untested"),
    fail_at=1.5,
)


def test_choice_questions_ask_a_choice_with_the_configured_options():
    questions = questions_for(CHUNK, CheckCatalog((custom_check(LICENSE),)))
    assert questions["custom_license"] == {
        "type": "choice",
        "instructions": LICENSE.question,
        "criteria": LICENSE.options,
    }
    assert "custom_license_kind" not in questions and "custom_license_line" in questions


def test_score_questions_ask_a_score_with_the_configured_levels():
    questions = questions_for(CHUNK, CheckCatalog((custom_check(COVERAGE),)))
    assert questions["custom_tests"] == {
        "type": "score",
        "instructions": COVERAGE.question,
        "criteria": ["Fully tested", "Partly tested", "Untested"],
    }


def choice_answer(**probabilities):
    return {"custom_license": {"type": "choice", "probabilities": probabilities}}


def test_a_choice_question_fails_on_the_combined_probability_of_flagged_options():
    checks = CheckCatalog((custom_check(LICENSE),))
    # Neither flagged option wins on its own, but together they are likely.
    [finding] = findings_for(CHUNK, choice_answer(none=0.4, mit=0.0, gpl=0.35, unknown=0.25), checks, 0.5)
    assert finding.probability == pytest.approx(0.6)
    assert finding.kind.label == "gpl"
    assert findings_for(CHUNK, choice_answer(none=0.9, mit=0.05, gpl=0.03, unknown=0.02), checks, 0.5) == []


def test_a_score_question_fails_at_or_above_fail_at():
    checks = CheckCatalog((custom_check(COVERAGE),))
    [finding] = findings_for(CHUNK, {"custom_tests": {"score": 1.8, "confidence": 0.7}}, checks, 0.5)
    assert (finding.score, finding.probability, finding.kind.label) == (1.8, 0.7, "Untested")
    assert findings_for(CHUNK, {"custom_tests": {"score": 1.2, "confidence": 0.9}}, checks, 0.5) == []


@pytest.mark.parametrize(
    "check, answer",
    [
        (LICENSE, {"custom_license": {"choice": "gpl"}}),
        (LICENSE, choice_answer(none=1.0)),
        (LICENSE, choice_answer(gpl="high", unknown=0.1)),
        (COVERAGE, {"custom_tests": {"score": 7}}),
        (COVERAGE, {"custom_tests": {"score": "bad"}}),
        (COVERAGE, {}),
    ],
)
def test_invalid_choice_and_score_answers_are_errors_not_passes(check, answer):
    with pytest.raises(ApiError, match="no valid answer"):
        findings_for(CHUNK, answer, CheckCatalog((custom_check(check),)), 0.5)


def test_custom_checks_keep_their_scope():
    assert custom_check(replace(DEBUG, scope="pr")).scope == "pr"
    assert custom_check(DEBUG).scope == "diff"


def test_pr_state_summarizes_the_whole_pull_request():
    from lisa.checks import MAX_PR_FILES, pr_state

    files = [{"filename": f"docs/page{i}.md", "status": "added", "additions": i, "deletions": 0} for i in range(200)]
    files.append({"filename": "tools/lib/git.ts", "status": "added", "additions": 500, "deletions": 0})
    state = pr_state("docs: tidy", "Moves pages.​", files)
    assert state["title"] == "docs: tidy" and state["description"] == "Moves pages.<U+200B>"
    assert state["summary"] == f"201 files changed, {sum(range(200)) + 500} lines added, 0 lines removed"
    assert state["directories"][0].startswith("docs: 200 files")
    assert state["files"][0] == "added +500 -0 tools/lib/git.ts"
    assert len(state["files"]) == MAX_PR_FILES + 1 and state["files"][-1] == "(and 51 smaller files)"


def test_pr_state_truncates_long_descriptions():
    from lisa.checks import MAX_PR_DESCRIPTION_CHARS, pr_state

    state = pr_state("t", "x" * (MAX_PR_DESCRIPTION_CHARS + 50), [])
    assert state["description"].endswith("(truncated)")
    assert state["summary"] == "0 files changed, 0 lines added, 0 lines removed"
