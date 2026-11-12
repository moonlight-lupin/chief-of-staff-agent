"""Hermes-native surface: read-only tools for the agent, /cos for the human.

The split is the plugin's invariant made structural: the model may observe and
propose; only a person may approve. So:

* every tool is read-only (capabilities, daily, review queue, preview,
  pipeline, spend status) and none can approve, dismiss, execute or claim;
* /cos approve and /cos dismiss run only for the owner's own surfaces
  (cli, tui, desktop, local) or for chat users listed in
  approvals.chat_approvers; anything else, including an unknown platform or
  a messaging command without a source, is refused (fail closed);
* handlers never raise: tools return JSON, /cos returns text.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest
import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import hermes_surface as hs  # noqa: E402
from test_hermes_catalog import _load_plugin_package  # noqa: E402

MANIFEST = yaml.safe_load((PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8"))
MUTATING = ("approve", "dismiss", "execute", "claim", "record-execution", "propose", "reserve", "record",
            "push", "install", "--fix")


class Ctx:
    def __init__(self):
        self.tools, self.commands = {}, {}

    def register_skill(self, *_):
        pass

    def register_hook(self, *_):
        pass

    def register_session_env(self, *_):
        pass

    def register_tool(self, *, name, toolset, schema, handler, **_):
        self.tools[name] = (toolset, schema, handler)

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = (handler, description, args_hint)


@pytest.fixture
def calls(monkeypatch):
    """Record script runs instead of spawning them."""
    seen = []

    def fake_run(script, args, timeout=hs.DEFAULT_TIMEOUT):
        seen.append((script, list(args)))
        return {"ok": True, "exit_code": 0, "stdout": json.dumps({"fake": script}), "stderr": ""}

    monkeypatch.setattr(hs, "run_script", fake_run)
    return seen


@pytest.fixture
def owner_config(monkeypatch):
    cfg = {"user": {"name": "Alicia Tan"}, "assistant": {"name": "Ada"},
           "approvals": {"chat_approvers": {"telegram:4242": "Alicia Tan"}}}
    monkeypatch.setattr(hs, "_load_config", lambda: cfg)
    return cfg


# ------------------------------------------------------------- registration


def test_register_exposes_the_declared_tools_and_the_cos_command():
    plugin = _load_plugin_package("cos_surface_reg")
    ctx = Ctx()
    plugin.register(ctx)
    assert sorted(ctx.tools) == sorted(MANIFEST["provides_tools"])
    assert {t[0] for t in ctx.tools.values()} == {hs.TOOLSET}
    assert "cos" in ctx.commands and ctx.commands["cos"][1]


def test_register_tolerates_a_hermes_without_tools_or_commands():
    plugin = _load_plugin_package("cos_surface_old")

    class Bare:
        def register_skill(self, *_):
            pass

        def register_hook(self, *_):
            pass

    plugin.register(Bare())


def test_schemas_are_well_formed():
    for spec in hs.TOOLS:
        schema = spec.schema
        assert schema["name"] == spec.name and len(schema["description"]) > 40
        assert schema["parameters"]["type"] == "object"


# ------------------------------------------------------------ tools: safety


def _argv_for(spec):
    sample = {"action_id": "act-123", "state": "requested"}
    return spec.build({k: v for k, v in sample.items() if k in spec.schema["parameters"]["properties"]})


@pytest.mark.parametrize("spec", hs.TOOLS, ids=lambda s: s.name)
def test_no_tool_can_mutate(spec):
    script, argv = _argv_for(spec)
    assert not any(word in argv for word in MUTATING), f"{spec.name} runs a mutating command: {argv}"
    assert not any(word in spec.name for word in ("approve", "dismiss", "execute", "claim"))


def test_preview_rejects_an_id_that_looks_like_a_flag(calls):
    out = json.loads(hs.tool_handler("cos_preview")({"action_id": "--all"}))
    assert out["ok"] is False and calls == []


def test_preview_requires_an_id(calls):
    assert json.loads(hs.tool_handler("cos_preview")({}))["ok"] is False


def test_tool_returns_parsed_json(calls):
    out = json.loads(hs.tool_handler("cos_capabilities")({}))
    assert out["ok"] is True and out["result"] == {"fake": "chief_of_staff.py"}
    assert calls == [("chief_of_staff.py", ["capabilities"])]


def test_tool_never_raises(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("disk gone")

    monkeypatch.setattr(hs, "run_script", boom)
    out = json.loads(hs.tool_handler("cos_pipeline")({}))
    assert out["ok"] is False and "disk gone" in out["error"]


def test_run_script_hands_hermes_secrets_to_the_child(monkeypatch):
    agent = types.ModuleType("agent")
    scope = types.ModuleType("agent.secret_scope")
    scope.get_secret = lambda name, default=None: "k-123" if name == "COMPOSIO_MCP_KEY" else default
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", scope)
    seen = {}

    def fake_subprocess_run(cmd, **kw):
        seen.update(cmd=cmd, env=kw["env"], cwd=kw["cwd"])
        return types.SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(hs.subprocess, "run", fake_subprocess_run)
    hs.run_script("spend.py", ["status"])
    assert seen["env"]["COMPOSIO_MCP_KEY"] == "k-123"
    assert seen["cmd"][0] == sys.executable and seen["cmd"][1].endswith("shared/scripts/spend.py")
    assert Path(seen["cwd"]) == PLUGIN_ROOT


def test_capabilities_tool_runs_for_real(tmp_path, monkeypatch):
    from test_notion_mirror import _config

    cfg = _config(tmp_path)
    monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(cfg.source_path))
    out = json.loads(hs.tool_handler("cos_capabilities")({}))
    assert out["ok"] is True and isinstance(out["result"], dict)


# --------------------------------------------------------------- /cos: human


def test_help_and_unknown_subcommands_show_usage(calls):
    assert "/cos approve" in hs.cos_command("help")
    assert "/cos approve" in hs.cos_command("frobnicate")
    assert "/cos approve" in hs.cos_command('approve "unterminated')
    assert calls == []


def test_bare_cos_shows_the_briefing(calls):
    hs.cos_command("", platform="cli")
    assert calls == [("chief_of_staff.py", ["daily", "--summary"])]


@pytest.mark.parametrize("platform", sorted(hs.OWNER_SURFACES))
def test_owner_surfaces_can_approve(calls, owner_config, platform):
    hs.cos_command("approve act-123 looks right", platform=platform)
    ((script, argv),) = calls
    assert script == "review_queue.py"
    assert argv == ["approve", "--action-id", "act-123", "--approver", "Alicia Tan", "--reason", "looks right"]


def test_listed_chat_user_can_approve(calls, owner_config):
    src = types.SimpleNamespace(user_id="4242", chat_id="c1")
    hs.cos_command("approve act-123", platform="telegram", source=src)
    ((_, argv),) = calls
    assert argv[argv.index("--approver") + 1] == "Alicia Tan"
    assert "telegram" in argv[argv.index("--reason") + 1]


@pytest.mark.parametrize("platform,source", [
    ("telegram", None),                                              # no transport identity
    ("telegram", types.SimpleNamespace(user_id="999", chat_id="c")),  # not listed
    ("discord", types.SimpleNamespace(user_id="4242", chat_id="c")),  # listed on another platform only
    ("", None),                                                      # unknown surface
])
def test_everyone_else_is_refused(calls, owner_config, platform, source):
    out = hs.cos_command("approve act-123", platform=platform, source=source)
    assert calls == [] and "not allowed" in out


def test_dismiss_needs_a_reason(calls, owner_config):
    assert "reason" in hs.cos_command("dismiss act-123", platform="cli")
    assert calls == []
    hs.cos_command("dismiss act-123 duplicate of act-99", platform="cli")
    assert calls == [("review_queue.py", ["dismiss", "--action-id", "act-123", "--reason", "duplicate of act-99"])]


def test_approve_rejects_a_flag_like_id(calls, owner_config):
    assert "action id" in hs.cos_command("approve --all", platform="cli")
    assert calls == []


def test_cos_never_raises(monkeypatch, owner_config):
    monkeypatch.setattr(hs, "run_script", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert "boom" in hs.cos_command("review", platform="cli")


def test_owner_without_a_configured_name_is_recorded_as_operator(calls, monkeypatch):
    monkeypatch.setattr(hs, "_load_config", lambda: {})
    hs.cos_command("approve act-1", platform="desktop")
    ((_, argv),) = calls
    assert argv[argv.index("--approver") + 1] == "operator (desktop)"


# ------------------------------------------------- the assistant's own name


@pytest.mark.parametrize("name,expected", [
    ("Ada", ["ada", "cos"]),
    ("Ada Lovelace", ["ada_lovelace", "cos"]),
    ("  JARVIS-2 ", ["jarvis_2", "cos"]),
    ("Chief of Staff", ["cos"]),          # default name: /cos only
    ("", ["cos"]),
    ("小助", ["cos"]),                     # disallowed: never a command
    ("a" * 40, ["cos"]),                   # over 32 characters: disallowed
])
def test_command_names_follow_the_assistant_name(name, expected):
    assert hs.command_names({"assistant": {"name": name}}) == expected


def test_register_adds_the_named_command_alongside_cos(monkeypatch):
    plugin = _load_plugin_package("cos_surface_named")
    monkeypatch.setattr(hs, "_load_config", lambda: {"assistant": {"name": "Ada"}})
    ctx = Ctx()
    plugin.register(ctx)
    assert {"ada", "cos"} <= set(ctx.commands)
    assert "/ada" in ctx.commands["ada"][1]


def test_usage_speaks_the_assistant_name(calls):
    text = hs.cos_command("help", command="ada")
    assert "/ada approve" in text and "/cos" not in text


def test_named_handler_forwards_session_context(calls, owner_config):
    handler = hs.make_command_handler("ada")
    handler("approve act-7", platform="cli", session_id="s1", source=None)
    ((_, argv),) = calls
    assert argv[:3] == ["approve", "--action-id", "act-7"]


# --------------------------------------------------------------- renaming


def test_renaming_retires_the_old_command(calls, monkeypatch):
    cfg = {"assistant": {"name": "Ada"}, "user": {"name": "Alicia Tan"}}
    monkeypatch.setattr(hs, "_load_config", lambda: cfg)
    old = hs.make_command_handler("ada")
    cfg["assistant"]["name"] = "Bea"                       # user renamed the assistant
    out = old("approve act-1", platform="cli")
    assert calls == [] and "/bea" in out and "restart" in out.lower()


def test_cos_keeps_working_after_a_rename(calls, monkeypatch):
    cfg = {"assistant": {"name": "Bea"}, "user": {"name": "Alicia Tan"}}
    monkeypatch.setattr(hs, "_load_config", lambda: cfg)
    hs.make_command_handler("cos")("approve act-1", platform="cli")
    assert calls and calls[0][0] == "review_queue.py"


def test_the_new_name_is_registered_on_the_next_load(monkeypatch):
    plugin = _load_plugin_package("cos_surface_rename")
    monkeypatch.setattr(hs, "_load_config", lambda: {"assistant": {"name": "Bea"}})
    ctx = Ctx()
    plugin.register(ctx)
    assert {"bea", "cos"} == set(ctx.commands)


def test_large_json_output_is_parsed_whole(monkeypatch):
    """Long paths (CI's TMPDIR) push capabilities past any byte cap: never cut JSON."""
    big = {"paths": ["/very/long/path/" + "x" * 200 for _ in range(200)]}
    monkeypatch.setattr(hs.subprocess, "run",
                        lambda cmd, **kw: types.SimpleNamespace(returncode=0, stdout=json.dumps(big), stderr=""))
    out = json.loads(hs.tool_handler("cos_capabilities")({}))
    assert out["ok"] is True and out["result"] == big


def test_large_text_output_is_capped(monkeypatch):
    monkeypatch.setattr(hs.subprocess, "run",
                        lambda cmd, **kw: types.SimpleNamespace(returncode=0, stdout="line\n" * 20000, stderr=""))
    out = json.loads(hs.tool_handler("cos_review_queue")({}))
    assert out["ok"] is True and len(out["result"]) <= hs._MAX_TEXT + 200
    assert "truncated" in out["result"]
