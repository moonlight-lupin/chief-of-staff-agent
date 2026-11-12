"""The README's setup commands actually work on a clean install (P1.7/P1.8).

Found by walking the README on a fresh clone: its bootstrap example omitted
--user-principal, which m365 requires, so the first real command failed.
"""
from __future__ import annotations

import re
import shlex
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "shared" / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "shared" / "scripts"))

import bootstrap  # noqa: E402

README = (ROOT / "README.md").read_text(encoding="utf-8")


def _readme_bootstrap_argv() -> list[str]:
    block = re.search(r"python shared/scripts/bootstrap\.py \\\n(.*?)\n\n", README, re.S)
    assert block, "README has no bootstrap example"
    text = re.sub(r"#.*", "", block.group(1)).replace("\\\n", " ")
    argv = shlex.split(text)
    # Placeholders the reader fills in.
    return [a.replace("<directory-tenant-id>", "11111111-1111-1111-1111-111111111111")
             .replace("<application-client-id>", "22222222-2222-2222-2222-222222222222") for a in argv]


@pytest.fixture
def tmp_config_dir(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    shutil.copy2(ROOT / "shared" / "config" / "company.yaml.example", cfg / "company.yaml.example")
    monkeypatch.setattr(bootstrap, "CONFIG_DIR", cfg)
    monkeypatch.setattr(bootstrap, "run_checks", lambda *a, **k: [])
    return cfg


def test_readme_bootstrap_example_runs(tmp_config_dir, tmp_path, capsys):
    argv = _readme_bootstrap_argv() + ["--project-root", str(tmp_path / "proj")]
    assert bootstrap._main(argv) == 0, capsys.readouterr()
    assert (tmp_config_dir / "company.yaml").is_file()


def test_readme_names_the_storage_choice():
    assert "--storage local" in README and "--storage git --data-repo" in README


def test_readme_does_not_send_hermes_users_to_dotenv():
    install = README.split("## 🤖 Easiest install", 1)[1].split("\n## ", 1)[0]
    assert "Hermes" in install and "settings" in install


@pytest.mark.parametrize("command", [
    "python shared/scripts/chief_of_staff.py demo",
    "python shared/scripts/chief_of_staff.py demo --flow",
])
def test_readme_demo_commands_are_real(command):
    import chief_of_staff

    assert command in README
    args = command.split("chief_of_staff.py ", 1)[1].split()
    assert chief_of_staff.main(args) == 0
