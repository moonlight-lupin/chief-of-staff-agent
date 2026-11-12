"""Dependency and release security (P1.3).

Repository-level guarantees that otherwise erode one edit at a time:

* every GitHub Action is pinned to a full commit SHA, and workflows run with
  read-only tokens unless a job says otherwise;
* CI audits the declared dependencies (pinned pip-audit) and lints with a
  pinned ruff;
* requirements.txt and pyproject.toml declare the same bounded dependencies;
* Dependabot watches pip and the Actions pins; CODEOWNERS covers the
  security-sensitive paths;
* every manifest carries the release version;
* no secret, credential file or Chief-of-Staff user data is tracked.
"""
from __future__ import annotations

import json
import re
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.y*ml"))


def _workflow(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _tracked() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True)
    return [p for p in out.stdout.decode().split("\0") if p]


# ── workflows ───────────────────────────────────────────────────────────────

def test_there_are_workflows():
    assert WORKFLOWS


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_commit_sha(path):
    for line in path.read_text().splitlines():
        match = re.search(r"uses:\s*([^\s#]+)(\s*#\s*(\S+))?", line)
        if not match or match.group(1).startswith("./"):
            continue
        ref = match.group(1).split("@", 1)
        assert len(ref) == 2 and re.fullmatch(r"[0-9a-f]{40}", ref[1]), f"{path.name}: unpinned {match.group(1)}"
        assert match.group(3), f"{path.name}: say which version {ref[1][:7]} is (# vX.Y.Z)"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_tokens_are_read_only_by_default(path):
    data = _workflow(path)
    assert data.get("permissions") == {"contents": "read"}, f"{path.name}: set top-level permissions: contents: read"
    assert "write-all" not in path.read_text()


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_untrusted_pull_request_target(path):
    """pull_request_target runs fork code with a write token: not here."""
    triggers = _workflow(path).get(True) or _workflow(path).get("on") or {}
    assert "pull_request_target" not in triggers


def test_checkout_does_not_persist_credentials():
    for path in WORKFLOWS:
        data = _workflow(path)
        for job in (data.get("jobs") or {}).values():
            for step in job.get("steps") or []:
                if str(step.get("uses", "")).startswith("actions/checkout@"):
                    assert (step.get("with") or {}).get("persist-credentials") is False, path.name


def _ci_text() -> str:
    return "\n".join(p.read_text() for p in WORKFLOWS)


def test_ci_audits_dependencies_with_a_pinned_pip_audit():
    text = _ci_text()
    assert re.search(r"pip-audit==\d+\.\d+\.\d+", text)
    assert re.search(r"pip-audit\b.*-r requirements\.txt", text)


def test_ci_runs_the_repo_hygiene_tests():
    assert "pytest -q tests/test_supply_chain.py" in _ci_text()


def test_ruff_is_pinned():
    assert re.search(r"ruff==\d+\.\d+\.\d+", _ci_text())


# ── dependencies ────────────────────────────────────────────────────────────

def _requirements() -> dict[str, str]:
    reqs = {}
    for line in (ROOT / "requirements.txt").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            name = re.split(r"[<>=!~ ]", line, maxsplit=1)[0].lower()
            reqs[name] = line.replace(" ", "")
    return reqs


def _pyproject() -> dict[str, str]:
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    return {re.split(r"[<>=!~ ]", d, maxsplit=1)[0].lower(): d.replace(" ", "") for d in deps}


def test_requirements_match_pyproject_exactly():
    assert _requirements() == _pyproject()


def test_every_dependency_has_an_upper_bound():
    for spec in _pyproject().values():
        assert "<" in spec, f"{spec}: add an upper bound so a major release cannot land unreviewed"


# ── dependabot, codeowners ──────────────────────────────────────────────────

def test_dependabot_watches_pip_and_actions():
    data = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    ecosystems = {u["package-ecosystem"] for u in data["updates"]}
    assert {"pip", "github-actions"} <= ecosystems


def test_codeowners_covers_security_sensitive_paths():
    text = (ROOT / ".github" / "CODEOWNERS").read_text()
    rules = [ln.split()[0] for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    for path in ("/.github/", "/shared/scripts/approval_signing.py", "/shared/scripts/workspace_guardrails.py",
                 "/shared/scripts/state_db.py", "/shared/scripts/payload_policy.py", "/shared/scripts/spend.py",
                 "/requirements.txt", "/pyproject.toml"):
        assert path in rules, f"CODEOWNERS has no rule for {path}"


# ── release ─────────────────────────────────────────────────────────────────

def test_every_manifest_carries_the_release_version():
    version = yaml.safe_load((ROOT / "plugin.yaml").read_text())["version"]
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    assert plugin["version"] == version
    assert {p.get("version") for p in market.get("plugins", [])} == {version}


def test_changelog_names_the_release_version():
    version = yaml.safe_load((ROOT / "plugin.yaml").read_text())["version"]
    assert re.search(rf"^## v?{re.escape(str(version))}\b", (ROOT / "CHANGELOG.md").read_text(), re.M)


# ── tracked files ───────────────────────────────────────────────────────────

_SECRET_PATTERNS = {
    # Key material, not just the header: fixtures use a fake one-character body.
    "private key block": re.compile(r"-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----(\s|\\n)*[A-Za-z0-9+/=]{40,}"),
    "GitHub token": re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Slack token": re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{20,}\b"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    "Anthropic/OpenAI key": re.compile(r"\bsk-(ant-)?[A-Za-z0-9_\-]{32,}\b"),
    "service-account JSON": re.compile(r'"private_key"\s*:\s*"-----BEGIN [A-Z ]*KEY-----(\\n)?[A-Za-z0-9+/=]{40,}'),
}
_TEXT_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".json", ".toml", ".txt", ".sh", ".cfg", ".ini", ".example", ""}


def test_no_secret_is_tracked():
    hits = []
    for rel in _tracked():
        path = ROOT / rel
        if path.suffix not in _TEXT_SUFFIXES or not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        text = path.read_text(errors="ignore")
        for label, pattern in _SECRET_PATTERNS.items():
            if pattern.search(text):
                hits.append(f"{rel}: {label}")
    assert not hits, "possible secrets tracked:\n" + "\n".join(hits)


_FORBIDDEN = re.compile(
    r"(^|/)\.env(\..+)?$|\.(db|db-wal|db-shm|sqlite3?|pem|p12|pfx|key|kdbx)$|(^|/)id_(rsa|ed25519|ecdsa)$"
    r"|^shared/config/(company|companies|drive-map|queries)\.yaml$|^shared/config/companies/"
    r"|(^|/)\.cos-backups/|(^|/)\.audit/")
_USER_DATA = re.compile(r"^(pipeline|invoices|expenses|todos)\.yaml$|^wiki/")


def test_no_credential_file_or_user_data_is_tracked():
    bad = [p for p in _tracked() if _FORBIDDEN.search(p) or _USER_DATA.search(p)]
    assert not bad, f"must not be committed: {bad}"


def test_scanner_catches_real_key_material_and_ignores_fixtures():
    body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7" * 2
    real = f'"private_key": "-----BEGIN PRIVATE KEY-----\\n{body}\\n-----END PRIVATE KEY-----"'
    fake = '"private_key": "-----BEGIN PRIVATE KEY-----\\nx\\n-----END PRIVATE KEY-----"'
    assert _SECRET_PATTERNS["service-account JSON"].search(real)
    assert _SECRET_PATTERNS["private key block"].search(real)
    assert not any(p.search(fake) for p in _SECRET_PATTERNS.values())
    assert _SECRET_PATTERNS["GitHub token"].search("ghp_" + "a1B2" * 9)


def test_gitignore_covers_secrets_and_state():
    text = (ROOT / ".gitignore").read_text()
    for entry in (".env", "*.db", "shared/config/company.yaml", "skills.local/"):
        assert entry in text
