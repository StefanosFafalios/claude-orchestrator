import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = (ROOT / "skills" / "request-feature" / "SKILL.md").read_text()


def test_frontmatter_names_skill_and_describes_trigger():
    fm = re.match(r"---\n(.*?)\n---\n", SKILL, re.S).group(1)
    assert "name: request-feature" in fm
    desc = re.search(r"^description: (.+)$", fm, re.M).group(1)
    assert "orchestration" in desc and "not" in desc.lower()


def test_plugin_json_has_github_repository():
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+", manifest["repository"])


def test_skill_reads_repo_from_manifest_and_uses_gh_safely():
    assert "${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json" in SKILL
    assert "gh issue list --repo" in SKILL and "--search" in SKILL
    assert "gh issue create --repo" in SKILL and "--body-file -" in SKILL
    assert "github.com/" not in SKILL  # repo is never hard-coded
    assert "## Privacy" in SKILL and "public" in SKILL
