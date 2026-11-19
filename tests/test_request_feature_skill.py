import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = (ROOT / "skills" / "request-feature" / "SKILL.md").read_text()
CREATE = SKILL.index("gh issue create")


def test_frontmatter_names_skill_and_describes_trigger():
    fm = re.match(r"---\n(.*?)\n---\n", SKILL, re.S).group(1)
    assert "name: request-feature" in fm
    desc = re.search(r"^description: (.+)$", fm, re.M).group(1)
    assert "orchestration" in desc and "does not provide" in desc


def test_plugin_json_has_github_repository():
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+", manifest["repository"])


def test_skill_reads_repo_from_manifest_and_uses_gh_safely():
    assert "${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json" in SKILL
    assert "gh issue list --repo" in SKILL and "--search '<keywords>'" in SKILL
    assert "gh issue create --repo" in SKILL and "--body-file -" in SKILL
    assert "github.com/" not in SKILL  # repo is never hard-coded
    assert "never implements" in " ".join(SKILL.split())


def test_gh_missing_or_unauthenticated_prints_the_draft():
    assert "gh auth status" in SKILL and "gh auth login" in SKILL


def test_title_rules_and_duplicate_check_before_create():
    assert "72 characters or fewer" in SKILL
    assert SKILL.index("gh issue list") < CREATE


def test_model_text_never_reaches_the_shell_unquoted():
    assert '--title "<' not in SKILL and '"<keywords>"' not in SKILL
    assert "<<'ISSUE_TITLE'" in SKILL and "<<'ISSUE_BODY'" in SKILL and '--title "$title"' in SKILL


def test_only_the_repository_owner_skips_confirmation():
    assert "gh api user --jq .login" in SKILL
    owner = SKILL.index("gh api user --jq .login")
    assert owner < CREATE and "explicit yes" in SKILL[owner:CREATE]


def test_privacy_rules_and_recheck_right_before_create():
    assert "## Privacy" in SKILL and "public" in SKILL
    for item in ("project names", "paths", "file contents", "code", "URLs", "customer names", "secrets"):
        assert item in SKILL, item
    recheck = SKILL.index("Re-check privacy")
    assert SKILL.index("Draft") < recheck < SKILL.index("gh api user") < CREATE
