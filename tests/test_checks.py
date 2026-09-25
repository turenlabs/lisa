from dataclasses import replace

from lisa.checks import CHECKS, findings_for, questions_for, state_for
from lisa.diff import Chunk, Line

CHUNK = Chunk(
    file="src/db.py",
    diff='+ import os\n+ db.execute("SELECT * FROM users WHERE id = " + id)',
    added=[Line("+", "import os", 6), Line("+", 'db.execute("SELECT * FROM users WHERE id = " + id)', 7)],
    start_line=6,
)
SECURITY = next(check for check in CHECKS if check.key == "security")


def test_state_holds_the_file_and_its_diff():
    state = state_for(CHUNK)
    assert state["file"] == "src/db.py"
    assert state["diff"] == CHUNK.diff


def test_each_check_asks_a_gate_kind_and_line_question():
    questions = questions_for(CHUNK)
    for check in CHECKS:
        gate = questions[check.key]
        assert gate["type"] == "noul"
        assert gate["instructions"] == check.instructions
        assert set(gate["criteria"]) == {"true", "false"}
        assert questions[f"{check.key}_kind"]["type"] == "choice"
        assert set(questions[f"{check.key}_kind"]["criteria"]) == set(check.kinds)
        assert questions[f"{check.key}_line"]["criteria"] == {
            "6": "import os",
            "7": 'db.execute("SELECT * FROM users WHERE id = " + id)',
        }


def test_line_question_is_skipped_when_there_is_one_candidate_line():
    assert "security_line" not in questions_for(replace(CHUNK, added=CHUNK.added[:1]))


def test_every_kind_has_an_explanation_and_a_fix():
    for check in CHECKS:
        assert "other" in check.kinds
        for kind in check.kinds.values():
            assert kind.label and kind.criteria and kind.why and kind.fix


def test_yes_answers_become_findings_with_kind_and_line():
    answers = {
        "secret": {"type": "noul", "noul": 0.1},
        "security": {"type": "noul", "noul": 0.97},
        "security_kind": {"type": "choice", "choice": "injection"},
        "security_line": {"type": "choice", "choice": "7"},
        "complexity": {"type": "noul", "noul": 0.5},
    }
    findings = findings_for(CHUNK, answers, threshold=0.5)
    assert [(f.check.key, f.kind.label, f.line, f.probability) for f in findings] == [
        ("security", "Injection", 7, 0.97),
        ("complexity", "Unneeded complexity", 6, 0.5),
    ]
    assert findings[0].text.startswith("db.execute")


def test_threshold_controls_what_counts_as_yes():
    answers = {"security": {"type": "noul", "noul": 0.7}}
    assert findings_for(CHUNK, answers, threshold=0.8) == []
    assert len(findings_for(CHUNK, answers, threshold=0.6)) == 1


def test_malformed_answers_fall_back_instead_of_crashing():
    answers = {
        "security": {"noul": 0.9},
        "security_kind": {"choice": "not-a-kind"},
        "security_line": "garbage",
        "secret": {"noul": "yes"},
        "complexity": None,
    }
    [finding] = findings_for(CHUNK, answers, threshold=0.5)
    assert (finding.check, finding.kind.label, finding.line) == (SECURITY, "Security vulnerability", 6)
