import json
import re
from pathlib import Path

from claude_slack_bridge import __version__

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_plugin_manifest_matches_package_version() -> None:
    manifest = REPO_ROOT / "plugins" / "slack-bridge" / ".claude-plugin" / "plugin.json"
    plugin_version = json.loads(manifest.read_text())["version"]
    assert plugin_version == __version__, (
        f"plugin.json version {plugin_version!r} != package __version__ {__version__!r}. "
        "Bump both together or the daemon will spam version-mismatch warnings at users."
    )


def test_pyproject_matches_package_version() -> None:
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()
    match = re.search(r'^version = "([^"]+)"', pyproject, flags=re.MULTILINE)
    assert match, "pyproject.toml has no top-level version field"
    assert match.group(1) == __version__
