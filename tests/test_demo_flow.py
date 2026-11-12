"""The flagship demo (P1.5): the whole loop on sample data, in seven steps.

briefing → proposal → preview → approve (signed, as /cos approve does) →
simulated execution → audit → next briefing. Nothing leaves the machine, no
credentials are needed, nothing is written outside a throwaway directory
(unless --keep), and --reset removes only a directory the demo created.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "shared" / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "shared" / "scripts"))

import demo_flow  # noqa: E402

GOLDEN = ROOT / "tests" / "golden" / "demo_flow.txt"


def _run(**kw) -> tuple[int, str]:
    lines: list[str] = []
    rc = demo_flow.run_flow(out=lines.append, **kw)
    return rc, "\n".join(lines)


@pytest.fixture(autouse=True)
def _isolated_hermes_home(tmp_path, monkeypatch):
    """Stand-ins for every place a real Hermes home could be found."""
    home = tmp_path / "real-hermes-home"
    user_home = tmp_path / "user-home"
    for d in (home, user_home, tmp_path / "hermes-env-home"):
        d.mkdir()
    monkeypatch.setenv("CHIEF_OF_STAFF_HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-env-home"))
    monkeypatch.setenv("HOME", str(user_home))
    return home


def test_runs_all_seven_steps_in_order():
    rc, out = _run()
    assert rc == 0
    headers = re.findall(r"^Step (\d)/7 — (.+)$", out, re.M)
    assert [int(n) for n, _ in headers] == list(range(1, 8))
    titles = " ".join(t.lower() for _, t in headers)
    for word in ("briefing", "proposal", "preview", "approve", "execut", "audit", "next briefing"):
        assert word in titles


def test_the_approval_is_signed_and_the_action_runs_through_the_real_path():
    rc, out = _run()
    assert "signed" in out.lower()
    assert re.search(r"State: executed", out)
    for event in ("requested", "approved", "executing", "executed"):
        assert re.search(rf"\b{event}\b", out.split("Step 6/7")[1].split("Step 7/7")[0])


def test_nothing_leaves_the_machine():
    """No real client is built and no network library is called."""
    boom = AssertionError("network or real client used")
    with patch("workspace_client.get_workspace_client", side_effect=boom), \
         patch("requests.Session.request", side_effect=boom), \
         patch("requests.request", side_effect=boom), \
         patch("socket.create_connection", side_effect=boom):
        rc, out = _run()
    assert rc == 0
    assert "simulated" in out.lower()


def test_the_guarded_wrapper_consumes_the_grant():
    """Step 5 runs the real @guarded wrapper: the stand-in send is audited as a provider write."""
    rc, out = _run()
    assert rc == 0 and "grant consumed by the guarded send" in out


def test_step_one_is_the_real_briefing():
    rc, out = _run()
    step1 = out.split("Step 1/7")[1].split("Step 2/7")[0]
    assert "Good morning, Sample Co (DEMO)" in step1
    assert len(step1.splitlines()) > 10


def test_writes_nothing_outside_its_directory(_isolated_hermes_home, tmp_path):
    before = sorted(p.name for p in (ROOT / "examples").iterdir())
    rc, _ = _run()
    assert rc == 0
    assert sorted(p.name for p in (ROOT / "examples").iterdir()) == before
    for d in (_isolated_hermes_home, tmp_path / "hermes-env-home", tmp_path / "user-home"):
        assert list(d.iterdir()) == [], f"demo wrote into {d}"


def test_restores_the_environment(monkeypatch):
    monkeypatch.delenv("CHIEF_OF_STAFF_AUTO_APPROVE", raising=False)
    monkeypatch.setenv("CHIEF_OF_STAFF_HERMES_HOME", "/somewhere/else")
    _run()
    assert "CHIEF_OF_STAFF_AUTO_APPROVE" not in os.environ
    assert os.environ["CHIEF_OF_STAFF_HERMES_HOME"] == "/somewhere/else"


def test_break_glass_in_the_environment_is_not_needed_or_used(monkeypatch):
    """Even with break-glass set, the demo approves through a signature."""
    monkeypatch.setenv("CHIEF_OF_STAFF_AUTO_APPROVE", "1")
    rc, out = _run()
    assert rc == 0 and "signed" in out.lower()


def test_keep_and_reset(tmp_path):
    keep = tmp_path / "demo-state"
    rc, out = _run(keep=keep)
    assert rc == 0
    assert (keep / demo_flow.MARKER).is_file()
    assert (keep / "state.db").is_file()
    assert str(keep) in out
    assert demo_flow.reset(keep) == 0
    assert not keep.exists()


def test_keep_refuses_a_non_empty_directory_it_did_not_create(tmp_path):
    other = tmp_path / "mine"
    other.mkdir()
    (other / "important.txt").write_text("keep me")
    rc, out = _run(keep=other)
    assert rc != 0
    assert (other / "important.txt").read_text() == "keep me"


def test_reset_refuses_a_directory_without_the_marker(tmp_path):
    other = tmp_path / "mine"
    other.mkdir()
    (other / "important.txt").write_text("keep me")
    assert demo_flow.reset(other) != 0
    assert (other / "important.txt").exists()


def test_rerun_with_keep_never_deletes_files_the_user_added(tmp_path):
    keep = tmp_path / "demo-state"
    assert _run(keep=keep)[0] == 0
    (keep / "precious.txt").write_text("mine")
    rc, out = _run(keep=keep)
    assert rc != 0 and "precious.txt" in out
    assert (keep / "precious.txt").read_text() == "mine"
    assert demo_flow.reset(keep) != 0
    assert (keep / "precious.txt").exists()


def test_reset_refuses_a_stray_or_empty_marker(tmp_path):
    other = tmp_path / "mine"
    other.mkdir()
    (other / demo_flow.MARKER).write_text("")
    assert demo_flow.reset(other) != 0
    assert other.exists()


@pytest.mark.parametrize("where", ["home", "cwd", "plugin", "root"])
def test_reset_and_keep_refuse_protected_directories(where, tmp_path, monkeypatch):
    target = {"home": Path.home(), "cwd": Path.cwd(), "plugin": ROOT, "root": Path("/")}[where]
    with patch.object(demo_flow, "_owned_by_demo", return_value=None):
        assert demo_flow.reset(target) != 0
        assert _run(keep=target)[0] != 0
    assert target.exists()


def test_reset_refuses_a_symlink(tmp_path):
    keep = tmp_path / "demo-state"
    assert _run(keep=keep)[0] == 0
    link = tmp_path / "link"
    link.symlink_to(keep)
    assert demo_flow.reset(link) != 0
    assert keep.exists()


def test_keep_refuses_a_file(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x")
    rc, out = _run(keep=f)
    assert rc != 0 and f.read_text() == "x"


@pytest.mark.parametrize("argv", [["demo", "--keep", "x"], ["demo", "--flow", "--reset", "x"],
                                  ["demo", "--reset", "x", "--keep", "y"]])
def test_cli_rejects_flag_combinations(argv, capsys):
    import chief_of_staff

    assert chief_of_staff.main(argv) == 2


def _normalise(text: str) -> str:
    """Strip what changes run to run: ids, dates, times, keys, temp paths, the briefing body."""
    body = re.sub(r"(Step 1/7 — .+?\n).*?(?=\nStep 2/7)", r"\1<briefing>\n", text, flags=re.S)
    body = re.sub(r"\b[0-9a-f]{8}-[0-9a-f]{3}\b", "<id>", body)
    body = re.sub(r"\b[0-9a-f]{16}\b", "<key>", body)
    body = re.sub(r"\d{4}-\d{2}-\d{2}[T ][0-9:.+\-Z]+", "<ts>", body)
    body = re.sub(r"\d{4}-\d{2}-\d{2}", "<date>", body)
    body = re.sub(r"/[^\s'\"]*cos-demo-[^\s'\"]*", "<tmp>", body)
    return body.strip() + "\n"


def test_golden_output():
    rc, out = _run()
    assert rc == 0
    got = _normalise(out)
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(got)
    assert got == GOLDEN.read_text(), "demo output changed: review it, then UPDATE_GOLDEN=1 pytest tests/test_demo_flow.py"


def test_cli(capsys):
    import chief_of_staff

    assert chief_of_staff.main(["demo", "--flow"]) == 0
    assert "Step 7/7" in capsys.readouterr().out
