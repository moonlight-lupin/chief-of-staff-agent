"""Hermes plugin-catalog admission contract.

Each test maps to a numbered rule in NousResearch/hermes-agent
plugin-catalog/README.md:

* rule 6  - declared capabilities match what register() registers;
* rule 10 - dependencies carry an upper bound and are declared in plugin.yaml;
* rule 11 - credentials come through Hermes: declared as config_schema
  secrets, read with agent.secret_scope.get_secret in-process, handed to the
  plugin's own commands through ctx.register_session_env, never read from a
  .env file or a key file while running under Hermes;
* rule 12 - nothing waits for a person under cron or the gateway;
* rule 14 - requires_hermes is a SemVer floor declared in plugin.yaml.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import tomllib
import types
from pathlib import Path

import pytest
import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import plugin_secrets  # noqa: E402

MANIFEST = yaml.safe_load((PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8"))


def _load_plugin_package(name: str):
    pkg = types.ModuleType(name)
    pkg.__path__ = [str(PLUGIN_ROOT)]
    sys.modules[name] = pkg
    spec = importlib.util.spec_from_file_location(name, PLUGIN_ROOT / "__init__.py",
                                                  submodule_search_locations=[str(PLUGIN_ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fake_hermes(monkeypatch):
    """Install a stand-in for agent.secret_scope, as Hermes's process has it."""
    store: dict[str, str] = {}
    agent = types.ModuleType("agent")
    scope = types.ModuleType("agent.secret_scope")
    scope.get_secret = lambda name, default=None: store.get(name, default)
    agent.secret_scope = scope
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", scope)
    return store


# ------------------------------------------------------------------ rule 6


def test_declared_hooks_match_registered_hooks():
    plugin = _load_plugin_package("cos_catalog_hooks")
    registered = []

    class Ctx:
        def register_skill(self, *_):
            pass

        def register_hook(self, event, _cb):
            registered.append(event)

        def register_session_env(self, *_):
            pass

    plugin.register(Ctx())
    assert sorted(set(registered)) == sorted(MANIFEST["provides_hooks"])


def test_manifest_declares_no_required_env():
    assert MANIFEST.get("requires_env", []) == []   # every key is optional


# ----------------------------------------------------------------- rule 11


def test_every_plugin_secret_is_declared_in_config_schema():
    schema = MANIFEST["config_schema"]
    declared = {v["env"] for v in schema.values() if v.get("type") == "secret"}
    assert declared == set(plugin_secrets.SECRETS)
    for v in schema.values():
        if v.get("type") == "secret":
            assert v.get("description"), f"secret {v['env']} needs a description for the settings form"


def test_no_module_reads_a_plugin_secret_from_the_environment_directly():
    names = "|".join(map(re.escape, plugin_secrets.SECRETS))
    pattern = re.compile(rf"os\.(?:getenv|environ\.get|environ\[)\(?\s*[\"']({names})[\"']")
    offenders = []
    for path in [*SCRIPTS.rglob("*.py"), *PLUGIN_ROOT.glob("skills/*/scripts/*.py"),
                 PLUGIN_ROOT / "__init__.py", PLUGIN_ROOT / "hooks.py"]:
        if path.name == "plugin_secrets.py":
            continue
        for match in pattern.finditer(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(PLUGIN_ROOT)}: {match.group(1)}")
    assert not offenders, "read these through plugin_secrets.get():\n" + "\n".join(offenders)


def test_get_uses_hermes_secret_scope_in_process(fake_hermes, monkeypatch):
    monkeypatch.setenv("COMPOSIO_MCP_KEY", "from-process-env")
    fake_hermes["COMPOSIO_MCP_KEY"] = "from-hermes"
    assert plugin_secrets.get("COMPOSIO_MCP_KEY") == "from-hermes"


def test_get_reads_the_command_environment_outside_hermes(monkeypatch):
    monkeypatch.delitem(sys.modules, "agent.secret_scope", raising=False)
    monkeypatch.setenv("M365_CLIENT_SECRET", "s3cret")
    assert plugin_secrets.get("M365_CLIENT_SECRET") == "s3cret"


def test_session_env_resolver_hands_secrets_to_commands(fake_hermes):
    fake_hermes["DOCUSEAL_API_KEY"] = "doc-key"
    env = plugin_secrets.session_env("session-1")
    assert set(env) == set(plugin_secrets.SECRETS)
    assert env["DOCUSEAL_API_KEY"] == "doc-key"
    assert env["COMPOSIO_MCP_KEY"] is None          # unset -> removed from the command env


def test_register_wires_the_session_env(fake_hermes):
    plugin = _load_plugin_package("cos_catalog_env")
    calls = []

    class Ctx:
        def register_skill(self, *_):
            pass

        def register_hook(self, *_):
            pass

        def register_session_env(self, names, resolver):
            calls.append((list(names), resolver))

    plugin.register(Ctx())
    ((names, resolver),) = calls
    assert set(names) == set(plugin_secrets.SECRETS)
    assert resolver is plugin_secrets.session_env or resolver("s")  # callable


def test_register_tolerates_an_older_hermes_without_session_env():
    plugin = _load_plugin_package("cos_catalog_old")

    class Ctx:
        def register_skill(self, *_):
            pass

        def register_hook(self, *_):
            pass

    plugin.register(Ctx())   # no AttributeError


@pytest.mark.parametrize("marker", ["session", "in_process"])
def test_dotenv_is_never_read_under_hermes(tmp_path, monkeypatch, marker, fake_hermes):
    from config_loader import load_dotenv_file

    env = tmp_path / ".env"
    env.write_text("COS_CATALOG_PROBE=leaked\n")
    monkeypatch.delenv("COS_CATALOG_PROBE", raising=False)
    if marker == "session":
        monkeypatch.delitem(sys.modules, "agent.secret_scope")
        monkeypatch.setenv("HERMES_SESSION_ID", "abc")
    assert load_dotenv_file(env) == {}
    assert "COS_CATALOG_PROBE" not in __import__("os").environ


def test_dotenv_still_loads_standalone(tmp_path, monkeypatch):
    from config_loader import load_dotenv_file

    monkeypatch.delitem(sys.modules, "agent.secret_scope", raising=False)
    for key in [k for k in __import__("os").environ if k.startswith("HERMES_SESSION_")]:
        monkeypatch.delenv(key)
    env = tmp_path / ".env"
    env.write_text("COS_CATALOG_PROBE2=ok\n")
    monkeypatch.delenv("COS_CATALOG_PROBE2", raising=False)
    assert load_dotenv_file(env) == {"COS_CATALOG_PROBE2": "ok"}
    monkeypatch.delenv("COS_CATALOG_PROBE2")


SA_INFO = {"type": "service_account", "client_email": "bot@proj.iam.gserviceaccount.com",
           "private_key": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n",
           "token_uri": "https://oauth2.googleapis.com/token", "project_id": "proj"}


def test_google_service_account_json_secret_is_used_instead_of_a_file(monkeypatch):
    from providers import google_contacts

    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", json.dumps(SA_INFO))
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_PATH", raising=False)
    cfg = {"google": {"delegate_email": "ops@acme.example"}}
    source, delegate = google_contacts.service_account_settings(cfg)
    assert source == plugin_secrets.SA_SECRET_SOURCE and delegate == "ops@acme.example"
    assert google_contacts._rest_unavailable(cfg) is None


def test_sa_credentials_build_from_the_secret(monkeypatch):
    from providers import google_workspace

    seen = {}

    class FakeCreds:
        def with_subject(self, subject):
            seen["subject"] = subject
            return self

        def refresh(self, _req):
            seen["refreshed"] = True

    fake_sa = types.SimpleNamespace(
        Credentials=types.SimpleNamespace(
            from_service_account_info=lambda info, scopes: seen.update(info=info) or FakeCreds(),
            from_service_account_file=lambda *a, **k: pytest.fail("must not read a key file"),
        ))
    import google.oauth2

    monkeypatch.setitem(sys.modules, "google.oauth2.service_account", fake_sa)
    monkeypatch.setattr(google.oauth2, "service_account", fake_sa, raising=False)
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", json.dumps(SA_INFO))
    google_workspace._sa_credentials(service_account_path=plugin_secrets.SA_SECRET_SOURCE,
                                     delegate_email="ops@acme.example", scopes=["s"])
    assert seen["info"]["client_email"] == SA_INFO["client_email"]
    assert seen["subject"] == "ops@acme.example" and seen["refreshed"]


def test_key_file_is_refused_under_hermes(tmp_path, fake_hermes):
    from providers import google_workspace

    key = tmp_path / "sa.json"
    key.write_text(json.dumps(SA_INFO))
    with pytest.raises(RuntimeError, match="GOOGLE_SERVICE_ACCOUNT_JSON"):
        google_workspace._sa_credentials(service_account_path=str(key),
                                         delegate_email="ops@acme.example", scopes=["s"])


# ----------------------------------------------------------------- rule 10


def _deps() -> list[str]:
    return tomllib.loads((PLUGIN_ROOT / "pyproject.toml").read_text())["project"]["dependencies"]


def test_every_dependency_has_an_upper_bound():
    unbounded = [d for d in _deps() if "<" not in d]
    assert not unbounded, f"add an upper bound: {unbounded}"


def test_plugin_yaml_declares_the_same_python_dependencies():
    assert MANIFEST["python_dependencies"] == _deps()


# ----------------------------------------------------------------- rule 14


def test_requires_hermes_is_a_semver_floor():
    assert re.fullmatch(r">=\d+\.\d+\.\d+", MANIFEST["requires_hermes"])


# ----------------------------------------------------------------- rule 12


def test_self_sign_confirmation_refuses_without_a_terminal(monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("sign_pdf_cat", PLUGIN_ROOT / "skills/self-sign/scripts/sign_pdf.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("must not wait for input"))
    assert mod.confirm_locations([{"id": "a", "page": 1}]) == []
    assert "terminal" in capsys.readouterr().err


def test_interactive_onboarding_refuses_without_a_terminal(monkeypatch, tmp_path, capsys):
    import onboard

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("must not wait for input"))
    assert onboard.main(["--output", str(tmp_path / "company.yaml")]) == 2
    assert "--non-interactive" in capsys.readouterr().err


def test_m365_device_code_refuses_without_a_terminal(monkeypatch):
    """Device-code sign-in waits for a person: never under cron or the gateway."""
    from providers import m365_graph

    started = []
    fake_msal = types.SimpleNamespace(
        PublicClientApplication=lambda *a, **k: types.SimpleNamespace(
            initiate_device_flow=lambda **kw: started.append(kw) or {"user_code": "X"},
            acquire_token_by_device_flow=lambda flow: pytest.fail("must not wait for sign-in")))
    monkeypatch.setitem(sys.modules, "msal", fake_msal)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    client = m365_graph.M365GraphClient({"m365": {"auth": "device_code", "tenant_id": "t", "client_id": "c"}})
    with pytest.raises(RuntimeError, match="client_credentials"):
        client._get_token()
    assert started == []


# ─── Admission checks mirrored from hermes_cli/plugin_validate.py ──────────────
# Checked against NousResearch/hermes-agent at 72c880370f (2026-11-12). These
# are the static checks; the capability probe is covered above, and the
# install scanner (tools/plugin_guard.py) is run by `hermes plugins validate`.

_HERMES_CONFIG_TYPES = {"str", "string", "int", "integer", "float", "number", "bool", "boolean",
                        "list", "array", "dict", "object", "secret"}
_BINARY_MAGIC = (b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xce",
                 b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe", b"\x00asm")


def _is_pe(head: bytes) -> bool:
    """A Windows PE: "MZ", then "PE\\0\\0" at the offset stored at 0x3c (not just text starting MZ)."""
    if not head.startswith(b"MZ") or len(head) < 0x40:
        return False
    offset = int.from_bytes(head[0x3C:0x40], "little")
    return head[offset:offset + 4] == b"PE\0\0"


def test_pe_detection_needs_the_pe_header():
    assert not _is_pe(b"MZ is a postcode prefix" + b" " * 100)
    fake = bytearray(b"MZ" + b"\0" * 0x3A + (0x80).to_bytes(4, "little") + b"\0" * 0x40 + b"PE\0\0")
    assert _is_pe(bytes(fake))


def _tracked_files() -> list[Path]:
    import subprocess

    out = subprocess.run(["git", "ls-files", "-z"], cwd=PLUGIN_ROOT, capture_output=True, check=True).stdout
    return [PLUGIN_ROOT / p for p in out.decode().split("\0") if p]


def test_manifest_has_the_admission_fields():
    for key in ("name", "version", "description"):
        assert MANIFEST.get(key), key


def test_requires_hermes_parses_strictly():
    for clause in str(MANIFEST["requires_hermes"]).split(","):
        assert re.fullmatch(r"\s*(>=|<=|==|!=|>|<)?\s*\d+(\.\d+){0,2}\s*", clause), clause


def test_config_schema_types_are_ones_hermes_accepts():
    for key, spec in (MANIFEST.get("config_schema") or {}).items():
        assert isinstance(spec, dict), key
        assert str(spec.get("type", "str")).lower() in _HERMES_CONFIG_TYPES, key
        assert spec.get("required") in (None, True, False), key


def test_requires_env_is_upper_snake():
    for entry in MANIFEST.get("requires_env") or []:
        name = entry if isinstance(entry, str) else entry.get("name", "")
        assert re.fullmatch(r"[A-Z][A-Z0-9_]*", name), name


def test_rule_17_no_bytecode_or_native_binaries_are_tracked():
    bad = []
    for path in _tracked_files():
        if path.suffix in (".pyc", ".pyo"):
            bad.append(path.name)
        elif path.is_file():
            with path.open("rb") as fh:
                head = fh.read(4096)
            if any(head.startswith(m) for m in _BINARY_MAGIC) or _is_pe(head):
                bad.append(path.name)
    assert not bad, f"rule 17: unscannable payloads tracked: {bad}"


def test_no_tracked_file_exceeds_the_scanner_single_file_limit():
    big = [p.name for p in _tracked_files() if p.is_file() and p.stat().st_size > 1024 * 1024]
    assert not big, f"plugin_guard flags files over 1 MB: {big}"
