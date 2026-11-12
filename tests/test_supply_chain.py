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


_USES = re.compile(r"""uses:\s*['"]?([^\s#'"]+)['"]?\s*(?:#\s*(.*))?$""")


def _pin_problem(line: str) -> str | None:
    match = _USES.search(line)
    if not match or match.group(1).startswith("./"):
        return None
    ref, comment = match.group(1), (match.group(2) or "").strip()
    if ref.startswith("docker://"):
        return None if re.search(r"@sha256:[0-9a-f]{64}$", ref) else f"unpinned image {ref}"
    name, _, sha = ref.partition("@")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        return f"unpinned {ref}"
    if not re.match(r"v\d+(\.\d+)*\b", comment):
        return f"{name}@{sha[:7]}: say which version it is (# vX.Y.Z)"
    return None


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_commit_sha(path):
    problems = [p for line in path.read_text().splitlines() if (p := _pin_problem(line))]
    assert not problems, f"{path.name}: {problems}"


@pytest.mark.parametrize("line, ok", [
    ("- uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262  # v4.4.0", True),
    ("- uses: 'actions/checkout@11d5960a326750d5838078e36cf38b85af677262'  # v4.4.0", True),
    ("- uses: actions/checkout@v4", False),
    ("- uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262  # TODO", False),
    ("- uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262", False),
    ("- uses: docker://alpine@sha256:" + "a" * 64, True),
    ("- uses: docker://alpine:3", False),
    ("- uses: ./local-action", True),
])
def test_pin_checker(line, ok):
    assert (_pin_problem(line) is None) is ok


# Job-level write scopes that a reviewed change has allowed: (workflow, job, scope).
_ALLOWED_WRITES: set[tuple[str, str, str]] = set()


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_tokens_are_read_only_by_default(path):
    data = _workflow(path)
    assert data.get("permissions") == {"contents": "read"}, f"{path.name}: set top-level permissions: contents: read"
    assert "write-all" not in path.read_text()
    for job_name, job in (data.get("jobs") or {}).items():
        perms = job.get("permissions")
        if perms is None:
            continue
        assert isinstance(perms, dict), f"{path.name}/{job_name}: permissions must be a mapping"
        for scope, level in perms.items():
            if level == "write":
                assert (path.name, job_name, scope) in _ALLOWED_WRITES, f"{path.name}/{job_name}: {scope}: write"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_untrusted_pull_request_target(path):
    """pull_request_target runs fork code with a write token: not here."""
    triggers = _workflow(path).get(True) or _workflow(path).get("on") or {}
    names = set(triggers) if isinstance(triggers, (dict, list)) else {triggers}
    assert not names & {"pull_request_target", "workflow_run"}


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
    assert re.search(r"pip-audit\b.*floors\.txt.*--no-deps", text), "audit the minimum allowed versions too"


def test_ci_runs_the_repo_hygiene_tests():
    assert "pytest -q tests/test_supply_chain.py" in _ci_text()


def test_ruff_is_pinned():
    assert re.search(r"ruff==\d+\.\d+\.\d+", _ci_text())


# ── dependencies ────────────────────────────────────────────────────────────

def _name(spec: str) -> str:
    """PEP 503 normalised project name."""
    return re.sub(r"[-_.]+", "-", re.split(r"[<>=!~;\[ ]", spec, maxsplit=1)[0]).lower()


def _spec(spec: str) -> str:
    return spec.split(";", 1)[0].replace(" ", "")


def _requirements() -> dict[str, str]:
    lines = [ln.split("#", 1)[0].strip() for ln in (ROOT / "requirements.txt").read_text().splitlines()]
    return {_name(ln): _spec(ln) for ln in lines if ln}


def _pyproject() -> dict[str, str]:
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    return {_name(d): _spec(d) for d in deps}


def _plugin_yaml() -> dict[str, str]:
    deps = yaml.safe_load((ROOT / "plugin.yaml").read_text())["python_dependencies"]
    return {_name(d): _spec(d) for d in deps}


def test_requirements_match_pyproject_and_plugin_yaml_exactly():
    assert _requirements() == _pyproject() == _plugin_yaml()


def test_every_dependency_has_an_upper_bound():
    for spec in _pyproject().values():
        assert re.search(r"<\d", spec), f"{spec}: add an upper bound so a major release cannot land unreviewed"


def test_name_normalisation():
    assert _name("python_docx>=1") == _name("Python.Docx>=1") == "python-docx"
    assert not re.search(r"<\d", _spec('foo>=1; python_version<"3.13"'))


# ── dependabot, codeowners ──────────────────────────────────────────────────

def test_dependabot_watches_pip_and_actions():
    data = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    ecosystems = {u["package-ecosystem"] for u in data["updates"]}
    assert {"pip", "github-actions"} <= ecosystems


def test_codeowners_covers_security_sensitive_paths():
    text = (ROOT / ".github" / "CODEOWNERS").read_text()
    lines = [ln.split() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    assert all(len(parts) >= 2 and all(o.startswith("@") for o in parts[1:]) for parts in lines), \
        "every CODEOWNERS rule needs an @owner"
    rules = [parts[0] for parts in lines]
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
    "private key block": re.compile(r"-----BEGIN (RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----(\s|\\n)*[A-Za-z0-9+/=]{40,}"),
    "GitHub token": re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Slack token": re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{20,}\b"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    "Anthropic/OpenAI key": re.compile(r"\bsk-(ant-)?[A-Za-z0-9_\-]{32,}\b"),
    "Stripe live key": re.compile(r"\b(sk|rk)_live_[A-Za-z0-9]{20,}\b"),
    "Google OAuth client secret": re.compile(r"\bGOCSPX-[A-Za-z0-9_\-]{28}\b"),
    "Notion token": re.compile(r"\b(ntn_[A-Za-z0-9]{40,}|secret_[A-Za-z0-9]{43})\b"),
    "service-account JSON": re.compile(r'"private_key"\s*:\s*"-----BEGIN [A-Z ]*KEY-----(\\n)?[A-Za-z0-9+/=]{40,}'),
}
def _text(path: Path) -> str | None:
    """The file as text, or None for binaries (scan every tracked text file)."""
    if not path.is_file() or path.stat().st_size > 5_000_000:
        return None
    data = path.read_bytes()
    if b"\0" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def test_no_secret_is_tracked():
    hits = []
    for rel in _tracked():
        text = _text(ROOT / rel)
        if text is None:
            continue
        for label, pattern in _SECRET_PATTERNS.items():
            if pattern.search(text):
                hits.append(f"{rel}: {label}")
    assert not hits, "possible secrets tracked:\n" + "\n".join(hits)


_FORBIDDEN = re.compile(
    r"(^|/)\.env(\.(?!example$)[^/]+)?$|\.(db|sqlite3?|pem|p12|pfx|key|kdbx)$|\.db[-.][^/]*$"
    r"|(^|/)id_(rsa|ed25519|ecdsa)$"
    r"|^shared/config/(company|companies|drive-map|queries)\.yaml$|^shared/config/companies/"
    r"|(^|/)\.(cos-backups|cos-tmp|audit|runs|knowledge|integrations)/|(^|/)\.last_briefing$")
# A data repo's records, anywhere — except the shipped sample data and test fixtures.
_USER_DATA = re.compile(r"(^|/)(pipeline|invoices|expenses|todos)\.yaml$|(^|/)wiki/")
_SAMPLE_ROOTS = ("examples/", "tests/", "docs/", "skills/")


def _forbidden(path: str) -> bool:
    return bool(_FORBIDDEN.search(path)) or (bool(_USER_DATA.search(path)) and not path.startswith(_SAMPLE_ROOTS))


def test_no_credential_file_or_user_data_is_tracked():
    bad = [p for p in _tracked() if _forbidden(p)]
    assert not bad, f"must not be committed: {bad}"


@pytest.mark.parametrize("path, bad", [
    (".env", True), ("sub/.env.local", True), (".env.example", False),
    ("state.db", True), ("state.db-wal", True), ("state.db.bak", True), ("state.db-journal", True),
    ("data/.runs/x.json", True), ("data/.knowledge/m.json", True), (".integrations/t", True),
    ("data/.last_briefing", True), ("data/pipeline.yaml", True), ("proj/wiki/x.md", True),
    ("examples/pipeline.yaml", False), ("shared/config/company.yaml", True),
    ("shared/config/company.yaml.example", False), ("keys/id_ed25519", True),
])
def test_forbidden_paths(path, bad):
    assert _forbidden(path) is bad


def test_scanner_catches_real_key_material_and_ignores_fixtures():
    body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7" * 2
    real = f'"private_key": "-----BEGIN PRIVATE KEY-----\\n{body}\\n-----END PRIVATE KEY-----"'
    fake = '"private_key": "-----BEGIN PRIVATE KEY-----\\nx\\n-----END PRIVATE KEY-----"'
    assert _SECRET_PATTERNS["service-account JSON"].search(real)
    assert _SECRET_PATTERNS["private key block"].search(real)
    assert not any(p.search(fake) for p in _SECRET_PATTERNS.values())
    assert _SECRET_PATTERNS["GitHub token"].search("ghp_" + "a1B2" * 9)
    assert _SECRET_PATTERNS["Google OAuth client secret"].search("GOCSPX-" + "a1B2c3D4e5" * 2 + "f6G7h8I9")
    enc = "-----BEGIN ENCRYPTED PRIVATE KEY-----\n" + body
    assert _SECRET_PATTERNS["private key block"].search(enc)
    assert _SECRET_PATTERNS["Stripe live key"].search("sk_live_" + "a1B2" * 6)
    assert _SECRET_PATTERNS["Notion token"].search("ntn_" + "a1B2" * 11)


def test_gitignore_covers_secrets_and_state():
    text = (ROOT / ".gitignore").read_text()
    for entry in (".env", "*.db", "shared/config/company.yaml", "skills.local/"):
        assert entry in text
