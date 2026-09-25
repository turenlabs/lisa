import pytest

from lisa.default_checks import DEFAULT_CHECKS
from lisa.models import Check, CheckCatalog, Coverage, Kind, RepoConfig

OTHER = Kind("Other", "Anything else.", "Why.", "Fix.")


def check(key: str, **kinds: Kind) -> Check:
    return Check(key, key.title(), f"Is it {key}?", {}, "Which line?", {"other": OTHER, **kinds})


def test_default_catalog_has_the_built_in_checks_in_order():
    assert DEFAULT_CHECKS.keys() == ["secret", "security", "complexity", "prompt_injection"]
    assert len(DEFAULT_CHECKS) == 4


def test_catalog_lookup_by_key():
    assert DEFAULT_CHECKS["secret"].title == "Secret"
    assert "security" in DEFAULT_CHECKS and "nope" not in DEFAULT_CHECKS
    assert DEFAULT_CHECKS.get("nope") is None
    with pytest.raises(KeyError, match="known checks are secret, security"):
        DEFAULT_CHECKS["nope"]


def test_catalog_search_matches_keys_titles_questions_and_kinds_case_insensitively():
    assert [c.key for c in DEFAULT_CHECKS.search("INJECTION")] == ["security", "prompt_injection"]
    assert [c.key for c in DEFAULT_CHECKS.search("private key")] == ["secret"]
    assert [c.key for c in DEFAULT_CHECKS.search("dead or commented-out")] == ["complexity"]
    assert DEFAULT_CHECKS.search("no such thing") == []


def test_catalog_without_and_extended_return_new_catalogs():
    trimmed = DEFAULT_CHECKS.without(["complexity"])
    assert "complexity" not in trimmed and "complexity" in DEFAULT_CHECKS
    assert trimmed.extended([check("custom_x")]).keys()[-1] == "custom_x"


def test_catalog_rejects_duplicate_keys_and_checks_without_other():
    with pytest.raises(ValueError, match="Duplicate check keys: a"):
        CheckCatalog((check("a"), check("a")))
    no_other = Check("b", "B", "?", {}, "?", {"x": OTHER})
    with pytest.raises(ValueError, match="without an 'other' kind: b"):
        CheckCatalog((no_other,))


def test_every_default_check_has_explanations_for_each_kind():
    for default in DEFAULT_CHECKS:
        assert default.kind_instructions and default.line_instructions
        for kind in default.kinds.values():
            assert kind.label and kind.criteria and kind.why and kind.fix


def test_coverage_is_complete_only_when_nothing_was_skipped():
    assert Coverage(files_changed=3, files_reviewed=3).complete
    assert not Coverage(failed=["a.py:1"]).complete
    assert not Coverage(too_large=["big.sql"]).complete
    assert not Coverage(files_unlisted=5).complete


def test_repo_config_ignore_patterns_match_across_directories():
    config = RepoConfig(ignore=("docs/*", "*.generated.ts"))
    assert config.ignores("docs/guide/intro.md")
    assert config.ignores("src/api.generated.ts")
    assert not config.ignores("src/docs.py")
