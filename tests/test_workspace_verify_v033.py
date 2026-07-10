#!/usr/bin/env python3
"""Tests for v0.3.3 — per-capability workspace provider verification.

Covers workspace_verify.run_verification / format_report and the
connect_workspace.py --verify / --verify-writes CLI wiring, all against fake
WorkspaceClients (no network, no real provider).
"""

import json
import os
import sys
import warnings
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))

import workspace_verify as wv  # noqa: E402

AUTO = "CHIEF_OF_STAFF_AUTO_APPROVE"
DESTRUCTIVE = "CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE"


@pytest.fixture(autouse=True)
def clean_env():
    for key in (AUTO, DESTRUCTIVE):
        os.environ.pop(key, None)
    yield
    for key in (AUTO, DESTRUCTIVE):
        os.environ.pop(key, None)


def _ok(data=None):
    return {"success": True, "data": data or {}, "error": None}


def _err(msg):
    return {"success": False, "data": {}, "error": msg}


class FakeClient:
    """Configurable fake WorkspaceClient covering the neutral surface.

    Read methods return their configured value; if that value is an exception
    instance it is used to emit a warning (mirroring the provider warn+return[]
    convention) or raised, per ``warn_reads``. Write methods return configured
    ActionResult-shaped dicts and record the env snapshot at call time.
    """

    def __init__(self, provider_name="fake", supports_map=None, healthy=True,
                 reads=None, warn_reads=None, writes=None):
        self._provider_name = provider_name
        self._supports = supports_map if supports_map is not None else {}
        self._healthy = healthy
        self._reads = reads or {}
        self._warn_reads = warn_reads or {}
        self._writes = writes or {}
        self.calls = []          # list of (method, args) tuples
        self.env_snapshots = []  # env at each WRITE call

    # capability
    @property
    def provider_name(self):
        return self._provider_name

    def supports(self, action):
        return self._supports.get(action, True)

    def health_check(self):
        self.calls.append(("health_check", ()))
        if isinstance(self._healthy, Exception):
            raise self._healthy
        return self._healthy

    # reads
    def _read(self, name):
        self.calls.append((name, ()))
        if name in self._warn_reads:
            warnings.warn(self._warn_reads[name])
            return []
        val = self._reads.get(name, [])
        if isinstance(val, Exception):
            raise val
        return val

    def mail_search(self, query, max_results=10):
        key = "mail_folder_scoped" if query == "in:inbox" else "mail_read"
        return self._read(key)

    def mail_list_tags(self):
        return self._read("mail_tags_list")

    def calendar_list(self, start, end):
        return self._read("calendar_read")

    def files_search(self, query, max_results=10):
        return self._read("files_read")

    # writes
    def _snapshot(self):
        self.env_snapshots.append({
            "auto": os.environ.get(AUTO),
            "destructive_present": DESTRUCTIVE in os.environ,
        })

    def _write(self, name, args):
        self.calls.append((name, args))
        self._snapshot()
        val = self._writes.get(name, _ok())
        if isinstance(val, Exception):
            raise val
        return val

    def mail_create_draft(self, to, subject, body, cc=None):
        return self._write("mail_create_draft", (to, subject))

    def mail_create_tag(self, name):
        return self._write("mail_create_tag", (name,))

    def mail_tag(self, message_id, tag_id):
        return self._write("mail_tag", (message_id, tag_id))

    def mail_trash(self, message_id):
        return self._write("mail_trash", (message_id,))

    def files_upload(self, file_path, parent_id=None):
        return self._write("files_upload", (file_path,))

    def files_trash(self, file_id):
        return self._write("files_trash", (file_id,))

    def called(self, method):
        return [c for c in self.calls if c[0] == method]


def _patch_client(monkeypatch, client):
    monkeypatch.setattr(wv, "get_workspace_client", lambda config: client)


# ── read verification ──────────────────────────────────────────────────────

def test_all_pass_read_run(monkeypatch):
    client = FakeClient(reads={
        "mail_read": [{"id": "m1"}],
        "mail_folder_scoped": [{"id": "m2"}],
        "mail_tags_list": [{"id": "t1"}],
        "calendar_read": [{"id": "e1"}],
        "files_read": [{"id": "f1"}],
    })
    _patch_client(monkeypatch, client)
    rep = wv.run_verification({}, include_writes=False)

    assert rep["provider"] == "fake"
    for name in ("auth", "mail_read", "mail_folder_scoped", "mail_tags_list",
                 "calendar_read", "files_read"):
        assert rep["checks"][name]["status"] == "pass", name
    assert rep["read_ready"] is True
    assert rep["write_ready"] == "partial"
    # writes untested
    for name in ("mail_draft", "mail_tag_write", "files_write",
                 "mail_send", "calendar_write"):
        assert rep["checks"][name]["status"] == "not_tested"


def test_empty_but_clean_list_is_pass(monkeypatch):
    # Empty lists, no warnings => pass (warning-capture semantics).
    client = FakeClient(reads={
        "mail_read": [], "mail_folder_scoped": [], "mail_tags_list": [],
        "calendar_read": [], "files_read": [],
    })
    _patch_client(monkeypatch, client)
    rep = wv.run_verification({}, include_writes=False)
    assert rep["checks"]["mail_read"]["status"] == "pass"
    assert "0 result" in rep["checks"]["mail_read"]["detail"]
    assert rep["read_ready"] is True


def test_partial_permission_files_read_fails(monkeypatch):
    # files_search warns (provider warn+return[] failure) => files_read fail,
    # others pass, read_ready still true.
    client = FakeClient(
        reads={"mail_read": [{"id": "m1"}], "calendar_read": [{"id": "e1"}]},
        warn_reads={"files_read": "m365 files_search failed: Graph API 403: Access denied"},
    )
    _patch_client(monkeypatch, client)
    rep = wv.run_verification({}, include_writes=False)
    assert rep["checks"]["files_read"]["status"] == "fail"
    assert "403" in rep["checks"]["files_read"]["detail"]
    assert rep["checks"]["mail_read"]["status"] == "pass"
    assert rep["read_ready"] is True  # files_read not required for read_ready


def test_read_failure_blocks_read_ready(monkeypatch):
    client = FakeClient(
        reads={"mail_read": [{"id": "m1"}]},
        warn_reads={"calendar_read": "m365 calendar_list failed: boom"},
    )
    _patch_client(monkeypatch, client)
    rep = wv.run_verification({}, include_writes=False)
    assert rep["checks"]["calendar_read"]["status"] == "fail"
    assert rep["read_ready"] is False


def test_auth_exception_is_isolated_fail(monkeypatch):
    # health_check raising must not stop later checks.
    client = FakeClient(healthy=RuntimeError("token error"),
                        reads={"mail_read": [{"id": "m1"}], "calendar_read": [{"id": "e1"}]})
    _patch_client(monkeypatch, client)
    rep = wv.run_verification({}, include_writes=False)
    assert rep["checks"]["auth"]["status"] == "fail"
    assert "token error" in rep["checks"]["auth"]["detail"]
    assert rep["checks"]["mail_read"]["status"] == "pass"
    assert rep["read_ready"] is False


# ── write smoke ─────────────────────────────────────────────────────────────

def test_write_smoke_happy_path(monkeypatch):
    client = FakeClient(
        supports_map={"mail.draft": True, "mail.create_tag": True,
                      "mail.tag": True, "files.upload": True},
        reads={"mail_read": [{"id": "m1"}], "calendar_read": [{"id": "e1"}]},
        writes={
            "mail_create_draft": _ok({"id": "draft-1"}),
            "mail_create_tag": _ok({"id": "CoS-Verify"}),
            "mail_tag": _ok({"id": "draft-1"}),
            "mail_trash": _ok({"id": "draft-1"}),
            "files_upload": _ok({"id": "file-1"}),
            "files_trash": _ok({"id": "file-1"}),
        },
    )
    _patch_client(monkeypatch, client)
    rep = wv.run_verification({"m365": {"user_principal": "op@x.com"}}, include_writes=True)

    assert rep["checks"]["mail_draft"]["status"] == "pass"
    assert rep["checks"]["mail_tag_write"]["status"] == "pass"
    assert rep["checks"]["files_write"]["status"] == "pass"
    assert rep["write_ready"] == "yes"

    # draft trashed, tag applied to draft, upload trashed
    assert client.called("mail_tag") == [("mail_tag", ("draft-1", "CoS-Verify"))]
    assert client.called("mail_trash") == [("mail_trash", ("draft-1",))]
    assert client.called("files_trash") == [("files_trash", ("file-1",))]
    # drafted to the configured operator/self address
    assert client.called("mail_create_draft")[0][1][0] == "op@x.com"

    # During every write call auto-approve was set and destructive never set.
    assert client.env_snapshots, "no write calls recorded"
    for snap in client.env_snapshots:
        assert snap["auto"] == "1"
        assert snap["destructive_present"] is False
    # auto-approve restored (was unset before the run)
    assert AUTO not in os.environ
    assert DESTRUCTIVE not in os.environ


def test_write_smoke_restores_preexisting_auto_approve(monkeypatch):
    os.environ[AUTO] = "preset"
    client = FakeClient(
        supports_map={"mail.draft": True, "mail.create_tag": True,
                      "mail.tag": True, "files.upload": True},
        writes={
            "mail_create_draft": _ok({"id": "d"}), "mail_create_tag": _ok({"id": "CoS-Verify"}),
            "mail_tag": _ok(), "mail_trash": _ok(),
            "files_upload": _ok({"id": "f"}), "files_trash": _ok(),
        },
    )
    _patch_client(monkeypatch, client)
    wv.run_verification({}, include_writes=True)
    assert os.environ[AUTO] == "preset"  # restored to prior value


def test_write_failure_sets_write_ready_no(monkeypatch):
    client = FakeClient(
        supports_map={"mail.draft": True, "mail.create_tag": True,
                      "mail.tag": True, "files.upload": True},
        reads={"mail_read": [{"id": "m1"}], "calendar_read": [{"id": "e1"}]},
        writes={
            "mail_create_draft": _ok({"id": "draft-1"}),
            "mail_create_tag": _ok({"id": "CoS-Verify"}),
            "mail_tag": _ok(), "mail_trash": _ok(),
            "files_upload": _err("Graph API 403: upload denied"),
        },
    )
    _patch_client(monkeypatch, client)
    rep = wv.run_verification({}, include_writes=True)
    assert rep["checks"]["files_write"]["status"] == "fail"
    assert "403" in rep["checks"]["files_write"]["detail"]
    assert rep["write_ready"] == "no"


def test_draft_no_id_fails(monkeypatch):
    client = FakeClient(
        supports_map={"mail.draft": True, "mail.create_tag": True,
                      "mail.tag": True, "files.upload": True},
        writes={"mail_create_draft": _ok({}),  # success but no id
                "files_upload": _ok({"id": "f"}), "files_trash": _ok()},
    )
    _patch_client(monkeypatch, client)
    rep = wv.run_verification({}, include_writes=True)
    assert rep["checks"]["mail_draft"]["status"] == "fail"
    # tag write cannot proceed without a draft
    assert rep["checks"]["mail_tag_write"]["status"] == "fail"
    assert rep["write_ready"] == "no"


def test_tag_already_exists_counts_as_pass(monkeypatch):
    client = FakeClient(
        supports_map={"mail.draft": True, "mail.create_tag": True,
                      "mail.tag": True, "files.upload": True},
        writes={
            "mail_create_draft": _ok({"id": "draft-1"}),
            "mail_create_tag": _err("category 'CoS-Verify' already exists"),
            "mail_tag": _ok(), "mail_trash": _ok(),
            "files_upload": _ok({"id": "f"}), "files_trash": _ok(),
        },
    )
    _patch_client(monkeypatch, client)
    rep = wv.run_verification({}, include_writes=True)
    assert rep["checks"]["mail_tag_write"]["status"] == "pass"
    # existing tag reused -> apply used the tag name
    assert client.called("mail_tag") == [("mail_tag", ("draft-1", "CoS-Verify"))]


def test_cleanup_failure_appends_detail_without_flipping_status(monkeypatch):
    client = FakeClient(
        supports_map={"mail.draft": True, "mail.create_tag": True,
                      "mail.tag": True, "files.upload": True},
        writes={
            "mail_create_draft": _ok({"id": "draft-1"}),
            "mail_create_tag": _ok({"id": "CoS-Verify"}),
            "mail_tag": _ok(),
            "mail_trash": _err("trash failed"),  # cleanup fails
            "files_upload": _ok({"id": "f"}),
            "files_trash": _err("trash failed"),  # cleanup fails
        },
    )
    _patch_client(monkeypatch, client)
    rep = wv.run_verification({}, include_writes=True)
    assert rep["checks"]["mail_tag_write"]["status"] == "pass"
    assert "cleanup" in rep["checks"]["mail_tag_write"]["detail"]
    assert rep["checks"]["files_write"]["status"] == "pass"
    assert "cleanup" in rep["checks"]["files_write"]["detail"]
    assert rep["write_ready"] == "yes"


def test_unsupported_capability_is_not_tested(monkeypatch):
    client = FakeClient(
        supports_map={"mail.draft": False, "mail.create_tag": False,
                      "mail.tag": False, "files.upload": False},
    )
    _patch_client(monkeypatch, client)
    rep = wv.run_verification({}, include_writes=True)
    assert rep["checks"]["mail_draft"]["status"] == "not_tested"
    assert rep["checks"]["mail_tag_write"]["status"] == "not_tested"
    assert rep["checks"]["files_write"]["status"] == "not_tested"
    # no tested write failed -> yes
    assert rep["write_ready"] == "yes"
    # no write methods were ever invoked
    assert client.called("mail_create_draft") == []
    assert client.called("files_upload") == []


def test_mail_send_and_calendar_write_always_not_tested(monkeypatch):
    client = FakeClient(
        supports_map={"mail.draft": True, "mail.create_tag": True,
                      "mail.tag": True, "files.upload": True},
        writes={
            "mail_create_draft": _ok({"id": "d"}), "mail_create_tag": _ok({"id": "CoS-Verify"}),
            "mail_tag": _ok(), "mail_trash": _ok(),
            "files_upload": _ok({"id": "f"}), "files_trash": _ok(),
        },
    )
    _patch_client(monkeypatch, client)
    rep = wv.run_verification({}, include_writes=True)
    assert rep["checks"]["mail_send"]["status"] == "not_tested"
    assert rep["checks"]["calendar_write"]["status"] == "not_tested"


def test_check_names_contract():
    assert wv.CHECK_NAMES == [
        "auth", "mail_read", "mail_folder_scoped", "mail_tags_list",
        "calendar_read", "files_read", "mail_draft", "mail_tag_write",
        "files_write", "mail_send", "calendar_write",
    ]


# ── format_report ───────────────────────────────────────────────────────────

def test_format_report_json_is_verbatim(monkeypatch):
    client = FakeClient(reads={"mail_read": [{"id": "m"}], "calendar_read": [{"id": "e"}]})
    _patch_client(monkeypatch, client)
    rep = wv.run_verification({}, include_writes=False)
    parsed = json.loads(wv.format_report(rep, fmt="json"))
    assert parsed == rep


def test_format_report_human_sections():
    rep = {
        "provider": "m365",
        "checks": {n: {"status": "pass", "detail": "ok"} for n in wv.CHECK_NAMES},
        "read_ready": True,
        "write_ready": "partial",
    }
    out = wv.format_report(rep, fmt="human")
    for section in ("Authentication", "Mail", "Calendar", "OneDrive/Files", "Writes"):
        assert section in out
    assert "provider: m365" in out
    assert "✓" in out


def test_format_report_markdown_table():
    rep = {
        "provider": "m365",
        "checks": {n: {"status": "not_tested", "detail": ""} for n in wv.CHECK_NAMES},
        "read_ready": False,
        "write_ready": "partial",
    }
    out = wv.format_report(rep, fmt="markdown")
    assert "| Check | Status | Detail |" in out
    assert "| auth | not_tested |" in out


# ── CLI wiring ──────────────────────────────────────────────────────────────

def test_cli_verify_human_exit0(monkeypatch, capsys):
    import connect_workspace as cw
    client = FakeClient(reads={"mail_read": [{"id": "m"}], "calendar_read": [{"id": "e"}]})
    _patch_client(monkeypatch, client)
    rc = cw.cmd_verify({}, include_writes=False, json_output=False)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Workspace verification" in out
    assert "Read ready:  yes" in out


def test_cli_verify_json_shape(monkeypatch, capsys):
    import connect_workspace as cw
    client = FakeClient(reads={"mail_read": [{"id": "m"}], "calendar_read": [{"id": "e"}]})
    _patch_client(monkeypatch, client)
    rc = cw.cmd_verify({}, include_writes=False, json_output=True)
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["provider"] == "fake"
    assert set(data["checks"].keys()) == set(wv.CHECK_NAMES)
    assert data["read_ready"] is True
    assert data["write_ready"] == "partial"


def test_cli_verify_read_fail_exit1(monkeypatch, capsys):
    import connect_workspace as cw
    client = FakeClient(warn_reads={"calendar_read": "calendar_list failed: boom"},
                        reads={"mail_read": [{"id": "m"}]})
    _patch_client(monkeypatch, client)
    rc = cw.cmd_verify({}, include_writes=False, json_output=True)
    capsys.readouterr()
    assert rc == 1


def test_cli_verify_writes_write_fail_exit1(monkeypatch, capsys):
    import connect_workspace as cw
    client = FakeClient(
        supports_map={"mail.draft": True, "mail.create_tag": True,
                      "mail.tag": True, "files.upload": True},
        reads={"mail_read": [{"id": "m"}], "calendar_read": [{"id": "e"}]},
        writes={
            "mail_create_draft": _ok({"id": "d"}), "mail_create_tag": _ok({"id": "CoS-Verify"}),
            "mail_tag": _ok(), "mail_trash": _ok(),
            "files_upload": _err("upload denied"),
        },
    )
    _patch_client(monkeypatch, client)
    rc = cw.cmd_verify({}, include_writes=True, json_output=True)
    data = json.loads(capsys.readouterr().out)
    # read_ready true but a tested write failed => exit 1
    assert data["read_ready"] is True
    assert data["write_ready"] == "no"
    assert rc == 1


def test_cli_verify_writes_happy_exit0(monkeypatch, capsys):
    import connect_workspace as cw
    client = FakeClient(
        supports_map={"mail.draft": True, "mail.create_tag": True,
                      "mail.tag": True, "files.upload": True},
        reads={"mail_read": [{"id": "m"}], "calendar_read": [{"id": "e"}]},
        writes={
            "mail_create_draft": _ok({"id": "d"}), "mail_create_tag": _ok({"id": "CoS-Verify"}),
            "mail_tag": _ok(), "mail_trash": _ok(),
            "files_upload": _ok({"id": "f"}), "files_trash": _ok(),
        },
    )
    _patch_client(monkeypatch, client)
    rc = cw.cmd_verify({}, include_writes=True, json_output=True)
    data = json.loads(capsys.readouterr().out)
    assert data["write_ready"] == "yes"
    assert rc == 0
