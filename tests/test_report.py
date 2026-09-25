from dataclasses import replace

import pytest

from lisa.default_checks import DEFAULT_CHECKS, DESCRIPTION_CHECK
from lisa.models import Coverage, Finding, ReviewResult
from lisa.report import code, marker, render_inline, render_summary

SECRET_CHECK, SECURITY_CHECK, COMPLEXITY_CHECK = (DEFAULT_CHECKS[key] for key in ("secret", "security", "complexity"))
SECRET = Finding(SECRET_CHECK, SECRET_CHECK.kinds["api_key"], "src/config.py", 12, 0.93, 'KEY = "sk-live-123"')
SQLI = Finding(SECURITY_CHECK, SECURITY_CHECK.kinds["injection"], "src/db.py", 7, 0.97, "db.execute(q + id)")
BLOB = "https://github.com/acme/app/blob/abc"


def review(findings, coverage, model="jev", checks=DEFAULT_CHECKS):
    return ReviewResult(checks, findings, coverage, model, "abc123def4567890", BLOB, "defaults")


def test_marker_ignores_line_numbers_but_not_content():
    assert marker(replace(SECRET, line=40)) == marker(SECRET)
    assert marker(replace(SECRET, text='KEY = "other"')) != marker(SECRET)


def test_inline_comment_explains_and_says_how_to_fix():
    body = render_inline(SECRET, "jev-1.13.0")
    assert body.startswith(marker(SECRET) + "\n")
    assert "**Secret: API key** (93% likely)" in body
    assert "**How to fix:** Revoke the key" in body
    assert "git history" in body
    assert "sk-live" not in body, "the secret itself is never repeated"


def test_only_secret_comments_carry_the_rotation_note():
    assert "git history" not in render_inline(SQLI, "jev")


def test_summary_lists_each_check_and_links_findings():
    body = render_summary("<!-- m -->", review([SECRET, SQLI], Coverage(3, 2, 4), "jev-1.13.0"))
    assert body.startswith("<!-- m -->\n## Lisa review: changes needed")
    assert "| Secret | **1 found** |" in body
    assert "| Security vulnerability | **1 found** |" in body
    assert "| Unneeded complexity | Clear |" in body
    assert "| Prompt injection | Clear |" in body
    assert (
        "Reviewed 2 of 3 changed files in 4 diff chunks at commit `abc123def456` using TypeSafe `jev-1.13.0`. "
        "Settings: defaults." in body
    )
    assert f"- [`src/db.py:7`]({BLOB}/src/db.py#L7) **Injection** (97%)." in body


def test_summary_passes_only_with_no_findings_and_full_coverage():
    assert "## Lisa review: passed" in render_summary("m", review([], Coverage(1, 1, 1)))
    partial = render_summary("m", review([], Coverage(2, 1, 1, failed=["a.py:3"], too_large=["big.sql"])))
    assert "## Lisa review: changes needed" in partial
    assert "### Not reviewed" in partial
    assert "`big.sql`" in partial and "`a.py:3`" in partial


def test_summary_stays_under_the_size_limit_on_massive_prs():
    kind = COMPLEXITY_CHECK.kinds["dead_code"]
    findings = [Finding(COMPLEXITY_CHECK, kind, f"src/file_{i}.py", i, 0.9) for i in range(5000)]
    body = render_summary("m", review(findings, Coverage(5000, 5000, 5000)), max_chars=20_000)
    assert len(body) <= 20_000
    assert "more findings not shown here" in body


def test_description_findings_are_explained_in_the_summary():
    kind = DESCRIPTION_CHECK.kinds["reviewer_manipulation"]
    finding = Finding(DESCRIPTION_CHECK, kind, "", 0, 0.91)
    body = render_summary("m", review([finding], Coverage(1, 1, 1)))
    assert "| Prompt injection | **1 found** |" in body
    assert f"- Pull request description: **Instructions to an AI reviewer** (91%). {kind.why} {kind.fix}" in body


def test_skipped_files_are_listed_without_failing_the_review():
    body = render_summary("m", review([], Coverage(2, 1, 1, skipped=["package-lock.json"])))
    assert "## Lisa review: passed" in body
    assert "Skipped by design" in body and "`package-lock.json`" in body


def test_findings_without_an_inline_comment_carry_their_explanation():
    unlocated = replace(SQLI, text="")
    body = render_summary("m", review([unlocated], Coverage(1, 1, 1)))
    assert unlocated.kind.why in body


@pytest.mark.parametrize(
    "text, rendered",
    [
        ("src/app.py", "`src/app.py`"),
        ("a`b.py", "`` a`b.py ``"),
        ("x``` [approve](https://evil)", "```` x``` [approve](https://evil) ````"),
        ("line\n## Lisa review: passed", "`line\\x0a## Lisa review: passed`"),
    ],
)
def test_code_spans_contain_untrusted_text(text, rendered):
    assert code(text) == rendered


def test_a_crafted_file_name_cannot_break_out_of_the_summary():
    evil = "x` [Click to approve](https://evil.example) @maintainers\n## Lisa review: passed `.py"
    finding = replace(SQLI, file=evil)
    body = render_summary("m", review([finding], Coverage(1, 1, 1, too_large=[evil])))
    for line in body.splitlines():
        assert not line.startswith("## Lisa review: passed")
    assert "[Click to approve](https://evil.example)" in body  # present, but only inside code spans
    assert body.count("``") >= 2


def test_score_findings_show_the_score_instead_of_a_probability():
    from lisa.checks import custom_check
    from lisa.models import CustomQuestion

    check = custom_check(
        CustomQuestion(id="t", question="Q?", title="Tests", type="score", levels=("Good", "Bad"), fail_at=0.5)
    )
    finding = Finding(check, check.kinds["level_1"], "a.py", 3, 0.8, "x = 1", score=0.9)
    assert "**Tests: Bad** (score 0.9 of 1)" in render_inline(finding, "jev")
    assert "**Bad** (score 0.9 of 1)." in render_summary(
        "m", review([finding], Coverage(1, 1, 1), checks=DEFAULT_CHECKS.extended([check]))
    )
