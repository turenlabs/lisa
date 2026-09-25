from dataclasses import replace

from lisa.default_checks import DEFAULT_CHECKS, DESCRIPTION_CHECK
from lisa.models import Coverage, Finding
from lisa.report import marker, render_inline, render_summary

SECRET_CHECK, SECURITY_CHECK, COMPLEXITY_CHECK = (DEFAULT_CHECKS[key] for key in ("secret", "security", "complexity"))
SECRET = Finding(SECRET_CHECK, SECRET_CHECK.kinds["api_key"], "src/config.py", 12, 0.93, 'KEY = "sk-live-123"')
SQLI = Finding(SECURITY_CHECK, SECURITY_CHECK.kinds["injection"], "src/db.py", 7, 0.97, "db.execute(q + id)")
BLOB = "https://github.com/acme/app/blob/abc"


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
    body = render_summary("<!-- m -->", DEFAULT_CHECKS, [SECRET, SQLI], Coverage(3, 2, 4), "jev-1.13.0", BLOB)
    assert body.startswith("<!-- m -->\n## Lisa review: changes needed")
    assert "| Secret | **1 found** |" in body
    assert "| Security vulnerability | **1 found** |" in body
    assert "| Unneeded complexity | Clear |" in body
    assert "| Prompt injection | Clear |" in body
    assert "Reviewed 2 of 3 changed files in 4 diff chunks using TypeSafe `jev-1.13.0`." in body
    assert f"- [`src/db.py:7`]({BLOB}/src/db.py#L7) **Injection** (97%)." in body


def test_summary_passes_only_with_no_findings_and_full_coverage():
    assert "## Lisa review: passed" in render_summary("m", DEFAULT_CHECKS, [], Coverage(1, 1, 1), "jev", BLOB)
    partial = render_summary(
        "m", DEFAULT_CHECKS, [], Coverage(2, 1, 1, failed=["a.py:3"], too_large=["big.sql"]), "jev", BLOB
    )
    assert "## Lisa review: changes needed" in partial
    assert "### Not reviewed" in partial
    assert "`big.sql`" in partial and "`a.py:3`" in partial


def test_summary_stays_under_the_size_limit_on_massive_prs():
    kind = COMPLEXITY_CHECK.kinds["dead_code"]
    findings = [Finding(COMPLEXITY_CHECK, kind, f"src/file_{i}.py", i, 0.9) for i in range(5000)]
    body = render_summary("m", DEFAULT_CHECKS, findings, Coverage(5000, 5000, 5000), "jev", BLOB, max_chars=20_000)
    assert len(body) <= 20_000
    assert "more findings not shown here" in body


def test_description_findings_are_explained_in_the_summary():
    kind = DESCRIPTION_CHECK.kinds["reviewer_manipulation"]
    finding = Finding(DESCRIPTION_CHECK, kind, "", 0, 0.91)
    body = render_summary("m", DEFAULT_CHECKS, [finding], Coverage(1, 1, 1), "jev", BLOB)
    assert "| Prompt injection | **1 found** |" in body
    assert f"- Pull request description: **Instructions to an AI reviewer** (91%). {kind.why} {kind.fix}" in body
