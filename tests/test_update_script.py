"""
Tests for scripts/update_nem_pd7day.sh, the installer attached to every release.

Each test serves a fake release tree over file:// (NEM_PD7DAY_RELEASE_BASE)
laid out as GitHub serves releases, and runs the real script with bash and
curl into a temporary config directory. The stamped copy is made with the
exact sed line from .github/workflows/release.yml, so a change to either side
that breaks the stamp fails here.

Run with:  python -m pytest tests/test_update_script.py -v
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from support import ROOT

SCRIPT = Path(ROOT) / "scripts" / "update_nem_pd7day.sh"
WORKFLOW = Path(ROOT) / ".github" / "workflows" / "release.yml"

pytestmark = pytest.mark.skipif(
    not (shutil.which("bash") and shutil.which("curl")), reason="needs bash and curl"
)


def _release_zip(version: str, *, manifest: bool = True) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("__init__.py", "")
        if manifest:
            zf.writestr("manifest.json", json.dumps({"domain": "nem_pd7day", "version": version}, indent=2))
    return buf.getvalue()


def _releases(tmp_path: Path, tags: dict[str, bytes], latest: str) -> Path:
    base = tmp_path / "releases"
    for tag, data in tags.items():
        (base / "download" / tag).mkdir(parents=True)
        (base / "download" / tag / "nem_pd7day.zip").write_bytes(data)
    (base / "latest" / "download").mkdir(parents=True)
    (base / "latest" / "download" / "nem_pd7day.zip").write_bytes(tags[latest])
    return base


def _run(script: Path, base: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script), *args],
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "NEM_PD7DAY_RELEASE_BASE": base.as_uri()},
        capture_output=True, text=True, timeout=60,
    )


def _installed(config: Path, where: str = "custom_components/nem_pd7day") -> str:
    return json.loads((config / where / "manifest.json").read_text())["version"]


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config"
    path.mkdir()
    return path


@pytest.fixture
def base(tmp_path):
    return _releases(tmp_path, {"v3.22.0": _release_zip("3.22.0"), "v3.23.0": _release_zip("3.23.0")}, "v3.23.0")


def test_fresh_install_of_latest(config, base):
    result = _run(SCRIPT, base, "latest", str(config))
    assert result.returncode == 0, result.stderr
    assert _installed(config) == "3.23.0"
    assert not (config / ".nem_pd7day.previous").exists()
    assert not (config / ".nem_pd7day.new").exists()
    assert "restart Home Assistant" in result.stdout


def test_update_keeps_the_previous_copy(config, base):
    assert _run(SCRIPT, base, "v3.22.0", str(config)).returncode == 0
    result = _run(SCRIPT, base, "3.23.0", str(config))       # a bare version gains its v
    assert result.returncode == 0, result.stderr
    assert _installed(config) == "3.23.0"
    assert _installed(config, ".nem_pd7day.previous") == "3.22.0"
    assert "installed 3.23.0 (was 3.22.0)" in result.stdout


def test_manifest_must_carry_the_version_asked_for(tmp_path, config):
    base = _releases(tmp_path, {"v3.23.0": _release_zip("3.22.0")}, "v3.23.0")
    result = _run(SCRIPT, base, "v3.23.0", str(config))
    assert result.returncode == 1
    assert "manifest.json says 3.22.0" in result.stderr
    assert not (config / "custom_components" / "nem_pd7day").exists()
    assert not (config / ".nem_pd7day.new").exists()


def test_a_failed_download_leaves_the_installed_copy(config, base):
    assert _run(SCRIPT, base, "v3.22.0", str(config)).returncode == 0
    result = _run(SCRIPT, base, "v9.9.9", str(config))
    assert result.returncode != 0
    assert _installed(config) == "3.22.0"
    assert not (config / ".nem_pd7day.previous").exists()


def test_a_zip_without_the_integration_is_refused(tmp_path, config):
    base = _releases(tmp_path, {"v3.23.0": _release_zip("3.23.0", manifest=False)}, "v3.23.0")
    result = _run(SCRIPT, base, "latest", str(config))
    assert result.returncode == 1
    assert "not the integration zip" in result.stderr


def test_bad_arguments(tmp_path, base):
    assert _run(SCRIPT, base, "main", str(tmp_path)).returncode == 2
    assert _run(SCRIPT, base, "latest", str(tmp_path / "missing")).returncode == 2


def _stamped(tmp_path: Path, version: str) -> Path:
    """The script as the release workflow attaches it, made with the workflow's own sed line."""
    match = re.search(r"sed '([^']+)' scripts/update_nem_pd7day\.sh", WORKFLOW.read_text())
    assert match, "release.yml no longer stamps the update script with sed"
    expression = match.group(1).replace("${{ inputs.version }}", version)
    out = tmp_path / "update_nem_pd7day.sh"
    out.write_text(subprocess.run(["sed", expression, str(SCRIPT)], capture_output=True, text=True, check=True).stdout)
    return out


def test_the_attached_copy_installs_its_own_release(tmp_path, config, base):
    stamped = _stamped(tmp_path, "3.22.0")
    assert 'STAMPED_VERSION="v3.22.0"' in stamped.read_text()
    result = _run(stamped, base, "", str(config))           # no version: the stamped one
    assert result.returncode == 0, result.stderr
    assert _installed(config) == "3.22.0"                   # not latest (3.23.0)


def test_the_repo_copy_installs_latest(config, base):
    assert SCRIPT.read_text().count("__RELEASE_VERSION__") == 1
    result = _run(SCRIPT, base, "", str(config))
    assert result.returncode == 0, result.stderr
    assert _installed(config) == "3.23.0"


def test_release_zip_layout_matches_what_the_workflow_builds():
    """The workflow zips custom_components/nem_pd7day from inside it, so the
    integration's files sit at the zip root, which is what the script checks."""
    assert "cd \"${{ github.workspace }}/custom_components/nem_pd7day\"" in WORKFLOW.read_text()
