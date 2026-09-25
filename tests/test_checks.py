from dataclasses import replace

import pytest

from lisa.checks import DESCRIPTION, build_checks, custom_check, findings_for, questions_for, state_for
from lisa.default_checks import DEFAULT_CHECKS, DESCRIPTION_CHECK
from lisa.errors import ConfigError
from lisa.models import CheckCatalog, Chunk, CustomQuestion, Line, RepoConfig

CHUNK = Chunk(
    file="src/db.py",
    diff='+ import os\n+ db.execute("SELECT * FROM users WHERE id = " + id)',
    added=(Line("+", "import os", 6), Line("+", 'db.execute("SELECT * FROM users WHERE id = " + id)', 7)),
    start_line=6,
)
DEBUG = CustomQuestion(
    id="debug-prints", question="Does this diff add debugging print statements?", title="Debug output", fix="Remove it."
)


def test_state_holds_the_file_and_its_diff():
    state = state_for(CHUNK)
    assert state["file"] == "src/db.py"
    assert state["diff"] == CHUNK.diff


def test_each_default_check_asks_a_gate_kind_and_line_question():
    questions = questions_for(CHUNK, DEFAULT_CHECKS)
    for check in DEFAULT_CHECKS:
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
    assert checks.keys() == ["secret", "security", "prompt_injection", "custom_debug-prints"]


def test_build_checks_rejects_unknown_check_names():
    with pytest.raises(ConfigError, match="unknown check.*complexty"):
        build_checks(RepoConfig(disabled=frozenset({"complexty"})))


def test_yes_answers_become_findings_with_kind_and_line():
    answers = {
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
    answers = {"security": {"type": "noul", "noul": 0.7}}
    assert findings_for(CHUNK, answers, DEFAULT_CHECKS, threshold=0.8) == []
    assert len(findings_for(CHUNK, answers, DEFAULT_CHECKS, threshold=0.6)) == 1


def test_a_check_threshold_overrides_the_review_threshold():
    strict = CheckCatalog((custom_check(replace(DEBUG, threshold=0.9)),))
    answers = {"custom_debug-prints": {"noul": 0.8}}
    assert findings_for(CHUNK, answers, strict, threshold=0.5) == []


def test_custom_findings_use_the_questions_explanation():
    checks = CheckCatalog((custom_check(DEBUG),))
    [finding] = findings_for(CHUNK, {"custom_debug-prints": {"noul": 0.9}}, checks, threshold=0.5)
    assert (finding.check.title, finding.kind.label, finding.kind.fix) == ("Debug output", "Debug output", "Remove it.")


def test_malformed_answers_fall_back_instead_of_crashing():
    answers = {
        "security": {"noul": 0.9},
        "security_kind": {"choice": "not-a-kind"},
        "security_line": "garbage",
        "secret": {"noul": "yes"},
        "complexity": None,
    }
    [finding] = findings_for(CHUNK, answers, DEFAULT_CHECKS, threshold=0.5)
    assert (finding.check.key, finding.kind.label, finding.line) == ("security", "Security vulnerability", 6)


def test_description_check_is_the_prompt_injection_check_worded_for_the_description():
    assert DESCRIPTION_CHECK.key == "prompt_injection"
    assert "`description`" in DESCRIPTION_CHECK.instructions
    assert set(questions_for(DESCRIPTION, CheckCatalog((DESCRIPTION_CHECK,)))) == {
        "prompt_injection",
        "prompt_injection_kind",
    }
