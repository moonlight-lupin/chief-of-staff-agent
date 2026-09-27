#!/usr/bin/env python3
"""Slice 2 tests: markdown section split, cmd_run archive hook, cross-producer A6.

Goldens in tests/fixtures/slice2_markdown_goldens.json were captured from
render_markdown before the section split. The join is ``\\n`` because the
pre-refactor renderer is ``"\\n".join(lines)`` and each body is a contiguous
slice of that same list.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
DAILY = PLUGIN_ROOT / "skills" / "daily-briefing" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))
if str(DAILY) not in sys.path:
    sys.path.insert(0, str(DAILY))

import briefing_attribution as ba  # noqa: E402

GOLDEN_PATH = Path(__file__).resolve().parent / "fixtures" / "slice2_markdown_goldens.json"
GOLDENS: dict[str, str] = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))

ANNOTATION = "> CHECK: confirm with bank\n"
BEGIN_RE = re.compile(
    r"<!-- cos:generated ([a-z0-9-]+) begin sha256=[0-9a-f]{12} -->"
)

FIXTURES: dict[str, dict] = {
    "empty": {
        "operator": "Test",
        "generated_at": "2026-09-27T08:00:00+00:00",
        "summary": {
            "needs_attention": 0,
            "pending_approvals": 0,
            "suggestions": 0,
            "classified_emails": 0,
            "system_warnings": 0,
        },
        "sections": {
            "needs_attention": [],
            "pending_approvals": {},
            "email_organisation": {},
            "calendar_deadlines": [],
            "recent_events": [],
            "suggested_next_actions": [],
            "system_health": {},
        },
    },
    "minimal": {
        "operator": "MH",
        "generated_at": "2026-09-27T08:00:00+00:00",
        "summary": {
            "needs_attention": 1,
            "pending_approvals": 0,
            "suggestions": 0,
            "classified_emails": 0,
            "system_warnings": 0,
        },
        "sections": {
            "needs_attention": [{"title": "Overdue", "risk": "high", "why": "Bank"}],
            "pending_approvals": {},
            "email_organisation": {},
            "calendar_deadlines": [],
            "recent_events": [],
            "suggested_next_actions": [],
            "system_health": {},
        },
    },
    "full": {
        "operator": "MH",
        "generated_at": "2026-09-27T08:00:00+00:00",
        "summary": {
            "needs_attention": 2,
            "pending_approvals": 2,
            "suggestions": 1,
            "classified_emails": 5,
            "system_warnings": 1,
        },
        "sections": {
            "needs_attention": [
                {"title": "Overdue", "risk": "high", "why": "Bank"},
                {"title": "Quiet", "risk": "low"},
            ],
            "pending_approvals": {
                "high": [{
                    "action_id": "a1",
                    "type": "gmail.send",
                    "summary": "Send",
                    "state": "requested",
                }],
                "medium": [{
                    "action_id": "a3",
                    "type": "drive.move",
                    "summary": "Move",
                    "state": "requested",
                }],
                "low": [{
                    "action_id": "a2",
                    "type": "gmail.label",
                    "summary": "Label",
                    "state": "approved",
                }],
            },
            "email_organisation": {
                "classified": 5,
                "unmapped": 2,
                "archive_candidates": 1,
                "label_suggestions": 0,
                "pending_actions": 0,
            },
            "calendar_deadlines": [
                {"when": "Tue 09:00", "summary": "Standup"},
                {"when": "Wed", "summary": "Filing"},
            ],
            "recent_events": [
                {"event_type": "email_received"},
                {"event_type": "calendar_event"},
            ],
            "suggested_next_actions": [
                {"title": "Review label", "risk": "low", "why": "Auto-suggested"},
            ],
            "system_health": {
                "state_files": "ok",
                "pending_summary": {"requested": 1, "approved": 1},
            },
            "knowledge_maintenance": {
                "wiki_pages_updated": 2,
                "wiki_pages_created": 1,
                "memory_records_created": 3,
                "memory_records_updated": 1,
                "observations_added": 4,
                "backlinks_added": 2,
                "duplicates_flagged": 1,
                "conflicts_flagged": 1,
                "open_questions_added": 1,
                "total_records": 9,
            },
            "bookkeeper": {
                "sources": {"invoices": {"fallback": "store", "reason": "not a mapping"}},
            },
            "pipeline": {"active_deals": 3, "stale_deals": 1},
        },
    },
    "recent_only": {
        "operator": "Op",
        "generated_at": "2026-09-27T08:00:00+00:00",
        "summary": {},
        "sections": {
            "recent_events": [{"event_type": "z"}, {"event_type": "a"}],
        },
    },
    "weekly": {
        "kind": "weekly",
        "generated_at": "2026-09-27T08:00:00+00:00",
        "week": {"start": "2026-09-01", "end": "2026-09-07"},
        "summary": {
            "deals_moved": 1,
            "invoices_sent": 2,
            "invoices_paid": 1,
            "overdue_invoices": 0,
            "tasks_completed": 4,
            "tasks_carry_over": 1,
            "wiki_pages_changed": 3,
        },
        "pipeline": {
            "total_deals": 2,
            "deals_moved": 1,
            "deals_by_stage": {"Lead": 1, "Paid": 1},
        },
        "bookkeeping": {
            "invoices_sent": 2,
            "invoices_received": 1,
            "invoices_paid": 1,
            "overdue_invoices": 0,
            "outstanding_ar": {"SGD": 40},
            "outstanding_ap": {"USD": 10},
        },
        "tasks": {
            "tasks_completed": 4,
            "tasks_carry_over": 1,
            "tasks_overdue_open": 0,
        },
        "knowledge": {
            "wiki_pages_created": 1,
            "wiki_pages_updated": 2,
            "wiki_pages_changed": 3,
        },
        "expenses": {"expenses": [{"id": "e1", "vendor": "Cafe", "amount": 12, "currency": "SGD"}]},
        "sources": {"invoices": {"fallback": "store", "reason": "not a mapping"}},
    },
}


def _company_yaml(root: Path, config_path: Path, **delivery: object) -> None:
    data = {
        "company": {
            "name": "Test Co",
            "jurisdiction": "SG",
            "incorporation_date": "2024-01-01",
            "financial_year_end": "31 Dec",
            "currency": "SGD",
        },
        "paths": {"project_root": str(root)},
        "delivery": {"default_format": "text", **delivery},
    }
    config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


@pytest.fixture()
def project(tmp_path, monkeypatch):
    root = tmp_path / "projroot"
    root.mkdir()
    config_path = tmp_path / "company.yaml"
    _company_yaml(root, config_path)
    monkeypatch.setenv("CHIEF_OF_STAFF_PROJECT_ROOT", str(root))
    monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config_path))
    monkeypatch.setattr(ba, "get_project_root", lambda config=None: root, raising=False)
    return root


def _run_cli(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SHARED_SCRIPTS / "briefing_attribution.py"), *argv],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        check=False,
        timeout=30,
    )


def _sections(briefing: dict) -> list[tuple[str, str]]:
    from briefing_renderer import render_markdown_sections

    return render_markdown_sections(briefing)


class TestJoinEquivalence:
    @pytest.mark.parametrize("name", list(FIXTURES))
    def test_render_markdown_matches_golden_and_section_join(self, name):
        from briefing_renderer import render_markdown, render_weekly_text

        briefing = FIXTURES[name]
        rendered = render_markdown(briefing)
        assert rendered == GOLDENS[name]
        sections = _sections(briefing)
        ids = [sid for sid, _body in sections]
        assert ids == list(dict.fromkeys(ids))
        assert set(ids) <= set(ba.BRIEFING_ARCHIVE_SECTIONS)
        if briefing.get("kind") == "weekly":
            assert sections == []
            assert rendered == render_weekly_text(briefing)
            return
        bodies = [body for _sid, body in sections]
        assert rendered == "\n".join(bodies)
        assert all(body.strip() for body in bodies)

    def test_full_daily_maps_only_blocks_the_renderer_emits(self):
        sections = dict(_sections(FIXTURES["full"]))
        assert list(sections) == [
            "header",
            "urgent",
            "pending-high",
            "pending-medium",
            "pending-low",
            "inbox-summary",
            "calendar",
            "todos",
            "finance",
            "footer",
        ]
        assert "## Calendar / Deadlines" in sections["calendar"]
        assert "## Recent Activity" in sections["todos"]
        assert sections["todos"].index("## Recent Activity") < sections["todos"].index(
            "## Suggested Next Actions"
        )
        assert "## System Health" in sections["finance"]
        assert "## Knowledge Maintenance" in sections["finance"]
        assert "Data divergence" in sections["finance"]
        assert "Data divergence" not in sections["footer"]
        assert "## System Health" not in sections["footer"]
        assert "deadlines" not in sections
        assert "pipeline" not in sections
        assert "all-clear" not in sections

    def test_empty_daily_is_header_and_footer_only(self):
        assert [sid for sid, _ in _sections(FIXTURES["empty"])] == ["header", "footer"]

    def test_recent_activity_stays_in_footer_when_nothing_follows_it(self):
        sections = dict(_sections(FIXTURES["recent_only"]))
        assert list(sections) == ["header", "footer"]
        assert "## Recent Activity" in sections["footer"]
        assert "- 1 a" in sections["footer"]
        assert "- 1 z" in sections["footer"]
        assert "## Recent Activity" not in sections["header"]


class TestCmdRunArchive:
    def _briefing(self) -> dict:
        briefing = dict(FIXTURES["minimal"])
        briefing["demo"] = True
        briefing["sections"] = dict(FIXTURES["minimal"]["sections"])
        return briefing

    def _run(self, project: Path, argv: list[str], briefing: dict, monkeypatch):
        import daily_briefing as db

        monkeypatch.setattr(
            db, "_build_structured_briefing", lambda *a, **k: briefing,
        )
        return db.main(["run", "--config", str(project.parent / "company.yaml"), *argv])

    def test_markdown_archives_and_delivery_stays_unmarked(self, project, monkeypatch, capsys):
        import briefing_attribution as attribution

        briefing = self._briefing()
        seen: list[dict] = []
        real = attribution.merge

        def wrapped(**kwargs):
            envelope = kwargs.get("envelope")
            assert isinstance(envelope, dict), kwargs
            seen.append(envelope)
            return real(**kwargs)

        monkeypatch.setattr(attribution, "merge", wrapped)
        rc = self._run(project, ["--markdown"], briefing, monkeypatch)
        captured = capsys.readouterr()
        assert rc == 0
        assert seen, "merge was not called with an envelope"
        envelope = seen[0]
        assert envelope["version"] == 1
        assert envelope["artifact"] == "briefing"
        assert envelope["generated_at"] == briefing["generated_at"]
        assert set(envelope["sections"]) <= set(ba.BRIEFING_ARCHIVE_SECTIONS)
        assert "Overdue" in envelope["sections"]["urgent"]
        assert not (project / ".cos-tmp").exists()
        archive = (project / "briefing.md").read_text(encoding="utf-8")
        assert "cos:generated" in archive
        assert "Overdue" in archive
        assert "cos:generated" not in captured.out
        assert "Overdue" in captured.out
        assert (project / ".cos-briefing-merge-log.jsonl").exists()

    def test_dry_run_writes_no_archive_and_still_prints_markdown(
        self, project, monkeypatch, capsys,
    ):
        import briefing_attribution as attribution

        def boom(**kwargs):
            raise AssertionError(f"merge called: {kwargs}")

        monkeypatch.setattr(attribution, "merge", boom)
        rc = self._run(
            project, ["--markdown", "--dry-run"], self._briefing(), monkeypatch,
        )
        captured = capsys.readouterr()
        assert rc == 0
        assert "Overdue" in captured.out
        assert "cos:generated" not in captured.out
        assert not (project / "briefing.md").exists()
        assert not (project / ".cos-tmp").exists()
        assert not (project / ".last_briefing").exists()
        assert not (project / ".cos-briefing-merge-log.jsonl").exists()

    def test_refusal_does_not_fail_the_run(self, project, monkeypatch, capsys):
        archive = project / "briefing.md"
        archive.write_text(
            "<!-- cos:generated urgent begin sha256=aaaaaaaaaaaa -->\nunclosed\n",
            encoding="utf-8",
        )
        before = archive.read_bytes()
        rc = self._run(project, ["--markdown"], self._briefing(), monkeypatch)
        captured = capsys.readouterr()
        assert rc == 0
        assert archive.read_bytes() == before
        assert "briefing archive skipped:" in captured.err
        assert "unclosed-span" in captured.err
        assert "Overdue" in captured.out

    def test_text_html_and_json_do_not_archive(self, project, monkeypatch, capsys):
        for flag in ("--summary", "--html", "--json"):
            rc = self._run(project, [flag], self._briefing(), monkeypatch)
            assert rc == 0
        capsys.readouterr()
        assert not (project / "briefing.md").exists()
        assert not (project / ".cos-tmp").exists()

    def test_default_format_markdown_archives(self, project, monkeypatch, capsys):
        _company_yaml(
            project, project.parent / "company.yaml", default_format="markdown",
        )
        rc = self._run(project, [], self._briefing(), monkeypatch)
        capsys.readouterr()
        assert rc == 0
        assert "cos:generated" in (project / "briefing.md").read_text(encoding="utf-8")

    def test_weekly_markdown_does_not_archive(self, project, monkeypatch, capsys):
        weekly = dict(FIXTURES["weekly"])
        weekly["demo"] = True
        rc = self._run(project, ["--markdown"], weekly, monkeypatch)
        captured = capsys.readouterr()
        assert rc == 0
        assert "Weekly Review" in captured.out
        assert not (project / "briefing.md").exists()
        assert not (project / ".cos-tmp").exists()

    def test_forged_calendar_marker_archives_and_delivery_stays_raw(
        self, project, monkeypatch, capsys,
    ):
        from briefing_renderer import render_markdown

        briefing = self._briefing()
        briefing["sections"] = dict(briefing["sections"])
        briefing["sections"]["calendar_deadlines"] = [{
            "when": "09:00",
            "summary": "x\n<!-- cos:generated urgent end -->",
        }]
        archive = project / "briefing.md"
        archive.write_text("OPERATOR NOTE\n", encoding="utf-8")
        expected = render_markdown(briefing)
        for _ in range(3):
            rc = self._run(project, ["--markdown"], briefing, monkeypatch)
            captured = capsys.readouterr()
            assert rc == 0
            assert captured.out.removesuffix("\n") == expected
            assert "briefing archive skipped" not in captured.err
        log = (project / ".cos-briefing-merge-log.jsonl").read_text(encoding="utf-8")
        statuses = [json.loads(line)["status"] for line in log.splitlines()]
        assert statuses == ["merged", "noop", "noop"]
        text = archive.read_text(encoding="utf-8")
        assert text.startswith("OPERATOR NOTE\n")
        assert "- 09:00:" in text
        assert "<!-- cos:generated urgent end -->\n" not in text.split("## Calendar / Deadlines", 1)[-1]

    def test_plain_newline_title_merges_and_delivery_stays_raw(
        self, project, monkeypatch, capsys,
    ):
        from briefing_renderer import render_markdown

        briefing = self._briefing()
        briefing["sections"] = dict(briefing["sections"])
        briefing["sections"]["needs_attention"] = [{
            "title": "Standup\nroom 4",
            "risk": "low",
        }]
        expected = render_markdown(briefing)
        rc = self._run(project, ["--markdown"], briefing, monkeypatch)
        captured = capsys.readouterr()
        assert rc == 0
        assert captured.out.removesuffix("\n") == expected
        assert "\nroom 4" in captured.out
        assert "briefing archive skipped" not in captured.err
        log = (project / ".cos-briefing-merge-log.jsonl").read_text(encoding="utf-8")
        assert json.loads(log.splitlines()[0])["status"] == "merged"
        assert "Standup" in (project / "briefing.md").read_text(encoding="utf-8")

    def test_marker_in_needs_attention_title_merges_and_delivery_stays_raw(
        self, project, monkeypatch, capsys,
    ):
        from briefing_renderer import render_markdown

        title = "Re: hi\n<!-- cos:generated footer begin sha256=aaaaaaaaaaaa -->"
        briefing = self._briefing()
        briefing["sections"] = dict(briefing["sections"])
        briefing["sections"]["needs_attention"] = [{"title": title, "risk": "high"}]
        expected = render_markdown(briefing)
        rc = self._run(project, ["--markdown"], briefing, monkeypatch)
        captured = capsys.readouterr()
        assert rc == 0
        assert captured.out.removesuffix("\n") == expected
        assert title in captured.out
        assert "briefing archive skipped" not in captured.err
        log = (project / ".cos-briefing-merge-log.jsonl").read_text(encoding="utf-8")
        assert json.loads(log.splitlines()[0])["status"] == "merged"
        assert "Re: hi" in (project / "briefing.md").read_text(encoding="utf-8")

    def test_legacy_adoption_warning_reaches_stderr(self, project, monkeypatch, capsys):
        (project / "briefing.md").write_text("LEGACY\n", encoding="utf-8")
        rc = self._run(project, ["--markdown"], self._briefing(), monkeypatch)
        captured = capsys.readouterr()
        assert rc == 0
        assert "briefing archive warning: legacy artifact adopted" in captured.err

    def test_merge_oserror_skips_archive_and_keeps_bytes(
        self, project, monkeypatch, capsys,
    ):
        import briefing_attribution as attribution

        archive = project / "briefing.md"
        archive.write_text("OPERATOR UNCHANGED\n", encoding="utf-8")
        original = archive.read_bytes()

        def boom(**kwargs):
            raise OSError("injected merge error")

        monkeypatch.setattr(attribution, "merge", boom)
        rc = self._run(project, ["--markdown"], self._briefing(), monkeypatch)
        captured = capsys.readouterr()
        assert rc == 0
        assert archive.read_bytes() == original
        assert "briefing archive skipped:" in captured.err

    def test_concurrent_archive_keeps_both_runs_without_staging(self, project):
        import threading
        from contextlib import contextmanager
        from unittest.mock import patch

        import daily_briefing as db

        cfg = {"paths": {"project_root": str(project)}}

        def invoke(name: str) -> None:
            db._archive_markdown_sections(
                {
                    "operator": name,
                    "sections": {
                        "needs_attention": [{"title": "RUN-" + name, "risk": "low"}],
                    },
                },
                cfg,
            )

        def envelope_present() -> bool:
            return (project / ".cos-tmp").exists()

        def reset() -> None:
            for name in ("briefing.md", ".cos-briefing-merge-log.jsonl", ".cos-briefing.lock"):
                (project / name).unlink(missing_ok=True)
            staged = project / ".cos-tmp"
            if staged.exists():
                for child in staged.iterdir():
                    child.unlink()
                staged.rmdir()

        def finish(results: dict, staged_at: list[str], later: str) -> None:
            assert set(results) == {"A", "B"}
            archive = (project / "briefing.md").read_text(encoding="utf-8")
            statuses = {name: results[name]["status"] for name in ("A", "B")}
            both_merged = statuses["A"] == "merged" and statuses["B"] == "merged"
            later_merged = statuses[later] == "merged" and f"RUN-{later}" in archive
            assert both_merged or later_merged, (statuses, archive)
            assert f"RUN-{later}" in archive
            assert staged_at == []
            assert not envelope_present()

        def run_merge_gate() -> None:
            reset()
            reached = {name: threading.Event() for name in ("A", "B")}
            release = {name: threading.Event() for name in ("A", "B")}
            results: dict = {}
            staged_at: list[str] = []
            real_merge = ba.merge

            def gated_merge(**kwargs):
                name = threading.current_thread().name
                reached[name].set()
                if envelope_present():
                    staged_at.append(name)
                assert release[name].wait(5)
                result = real_merge(**kwargs)
                results[name] = result
                return result

            with patch.object(ba, "merge", gated_merge):
                threads = [
                    threading.Thread(target=invoke, name=name, args=(name,))
                    for name in ("A", "B")
                ]
                threads[0].start()
                assert reached["A"].wait(5)
                threads[1].start()
                assert reached["B"].wait(5)
                release["A"].set()
                threads[0].join(5)
                release["B"].set()
                threads[1].join(5)
                assert not threads[0].is_alive() and not threads[1].is_alive()
            finish(results, staged_at, "B")

        def run_lock_gate() -> None:
            reset()
            reached = {name: threading.Event() for name in ("A", "B")}
            release = {name: threading.Event() for name in ("A", "B")}
            results: dict = {}
            staged_at: list[str] = []
            real_merge = ba.merge
            real_lock = ba._exclusive_lock

            @contextmanager
            def gated_lock(*args, **kwargs):
                if threading.current_thread().name == "A":
                    reached["A"].set()
                    if envelope_present():
                        staged_at.append("A")
                    assert release["A"].wait(5)
                with real_lock(*args, **kwargs) as acquired:
                    yield acquired

            def delayed_b_merge(**kwargs):
                name = threading.current_thread().name
                if name == "B":
                    reached["B"].set()
                    if envelope_present():
                        staged_at.append("B")
                    assert release["B"].wait(5)
                results[name] = real_merge(**kwargs)
                return results[name]

            with patch.object(ba, "_exclusive_lock", gated_lock), patch.object(ba, "merge", delayed_b_merge):
                threads = [
                    threading.Thread(target=invoke, name=name, args=(name,))
                    for name in ("A", "B")
                ]
                threads[0].start()
                assert reached["A"].wait(5)
                threads[1].start()
                assert reached["B"].wait(5)
                release["A"].set()
                threads[0].join(5)
                release["B"].set()
                threads[1].join(5)
                assert not threads[0].is_alive() and not threads[1].is_alive()
            finish(results, staged_at, "B")

        run_merge_gate()
        run_lock_gate()


class TestA6CrossProducer:
    def test_agent_then_python_on_annotated_archive(self, project):
        from briefing_renderer import render_markdown_sections

        archive = project / "briefing.md"
        archive.write_text(ANNOTATION, encoding="utf-8")
        env_dir = project / ".cos-tmp"
        env_dir.mkdir()
        env = env_dir / "briefing-sections.json"
        env.write_text(
            json.dumps({
                "version": 1,
                "generated_at": "2026-09-26T08:00:00+00:00",
                "artifact": "briefing",
                "sections": {
                    "urgent": "AGENT-URGENT-BODY",
                    "calendar": "AGENT-CALENDAR-BODY",
                    "pipeline": "AGENT-PIPELINE-BODY",
                },
            }),
            encoding="utf-8",
        )
        proc = _run_cli(["merge", "--artifact", "briefing", "--sections", str(env)])
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout)["status"] == "merged"
        assert not env.exists()

        text = archive.read_text(encoding="utf-8")
        assert text.startswith(ANNOTATION)
        end_line = "<!-- cos:generated urgent end -->\n"
        assert end_line in text
        archive.write_text(text.replace(end_line, end_line + "> STILL CHECK\n", 1), encoding="utf-8")

        py_briefing = {
            "operator": "MH",
            "generated_at": "2026-09-27T08:00:00+00:00",
            "summary": {
                "needs_attention": 1,
                "pending_approvals": 0,
                "suggestions": 0,
                "classified_emails": 0,
                "system_warnings": 0,
            },
            "sections": {
                "needs_attention": [{"title": "PYTHON-URGENT", "risk": "high", "why": "bank"}],
                "calendar_deadlines": [{"when": "Mon", "summary": "PYTHON-CAL"}],
            },
        }
        emitted = dict(render_markdown_sections(py_briefing))
        result = ba.merge(artifact="briefing", sections=emitted, config=None)
        assert result["status"] == "merged", result

        final = archive.read_text(encoding="utf-8")
        assert final.startswith(ANNOTATION)
        assert "> STILL CHECK\n" in final
        still_at = final.index("> STILL CHECK\n")
        urgent_end = final.index("<!-- cos:generated urgent end -->")
        assert urgent_end < still_at
        assert still_at < final.index("<!-- cos:generated", still_at)
        assert "AGENT-URGENT-BODY" not in final
        assert "AGENT-CALENDAR-BODY" not in final
        assert "AGENT-PIPELINE-BODY" not in final
        assert "PYTHON-URGENT" in final
        assert "PYTHON-CAL" in final
        assert "_(none today)_" in final
        ids = BEGIN_RE.findall(final)
        assert len(ids) == len(set(ids))
        assert {"urgent", "calendar", "pipeline"} <= set(ids)

        before = archive.read_bytes()
        again = ba.merge(artifact="briefing", sections=emitted, config=None)
        assert again["status"] == "noop"
        assert archive.read_bytes() == before
