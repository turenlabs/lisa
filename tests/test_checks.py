from dataclasses import replace

from lisa.checks import CHECKS, build_request, interpret
from lisa.diff import Chunk, Line

CHUNK = Chunk(
    file="src/db.py",
    diff='+ import os\n+ db.execute("SELECT * FROM users WHERE id = " + id)',
    added=[Line("+", "import os", 6), Line("+", 'db.execute("SELECT * FROM users WHERE id = " + id)', 7)],
    start_line=6,
)


def test_build_request_asks_a_gate_kind_and_line_question_per_check():
    request = build_request(CHUNK, "jev-latest")
    assert request["model"] == "jev-latest"
    assert request["state"]["diff"] == CHUNK.diff
    for key, check in CHECKS.items():
        gate = request["questions"][key]
        assert gate["type"] == "noul"
        assert gate["instructions"] == check.instructions
        assert set(gate["criteria"]) == {"true", "false"}
        kind = request["questions"][f"{key}_kind"]
        assert kind["type"] == "choice"
        assert set(kind["criteria"]) == set(check.kinds)
        line = request["questions"][f"{key}_line"]
        assert line["criteria"] == {"6": "import os", "7": 'db.execute("SELECT * FROM users WHERE id = " + id)'}


def test_line_question_is_skipped_when_there_is_one_candidate_line():
    request = build_request(replace(CHUNK, added=CHUNK.added[:1]), "jev-latest")
    assert "security_line" not in request["questions"]


def test_every_kind_has_explanation_and_fix():
    for check in CHECKS.values():
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
    findings = interpret(CHUNK, answers, threshold=0.5)
    assert [(f.check, f.kind, f.line, f.probability) for f in findings] == [
        ("security", "injection", 7, 0.97),
        ("complexity", "other", 6, 0.5),
    ]
    assert findings[0].details.label == "Injection"
    assert findings[0].text.startswith("db.execute")


def test_threshold_controls_what_counts_as_yes():
    answers = {"security": {"type": "noul", "noul": 0.7}}
    assert interpret(CHUNK, answers, threshold=0.8) == []
    assert len(interpret(CHUNK, answers, threshold=0.6)) == 1
