#!/usr/bin/env python3
"""Batch 2 adoption tests for Generated-Section Attribution.

Spec: .hermes/specs/2026-09-26_generated-section-attribution.md
(C-9, C-10, US-3, A15, A16). The helper module and its contract tests are
batch 1 and must not be modified. These tests cover skill adoption, the
doctor staleness check, and the plugin gitignore entries.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))

import briefing_attribution as ba  # noqa: E402

ANNOTATION = "> CHECK: confirm with bank\n"


@pytest.fixture()
def project(tmp_path, monkeypatch):
    """Isolated project root wired through config_loader's resolution path."""
    root = tmp_path / "projroot"
    root.mkdir()
    monkeypatch.setenv("CHIEF_OF_STAFF_PROJECT_ROOT", str(root))
    # config_loader.get_project_root needs a config; the env var is the
    # Settings-level fallback. Provide both so resolution never reads HOME.
    monkeypatch.setattr(
        ba, "get_project_root", lambda config=None: root, raising=False
    )
    config_path = tmp_path / "company.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "company": {
                    "name": "Test Co",
                    "jurisdiction": "SG",
                    "incorporation_date": "2024-01-01",
                    "financial_year_end": "31 Dec",
                    "currency": "SGD",
                },
                "paths": {"project_root": str(root)},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config_path))
    return root


def begin(id_: str, body: str, hash12: str | None = None) -> str:
    h = hash12 or ba.body_hash(body)
    return f"<!-- cos:generated {id_} begin sha256={h} -->"


def end(id_: str) -> str:
    return f"<!-- cos:generated {id_} end -->"


def marked(id_: str, body: str) -> str:
    return f"{begin(id_, body)}\n{body}\n{end(id_)}\n"


def _run_cli(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Invoke the real helper CLI. In-process monkeypatches do not apply."""
    return subprocess.run(
        [sys.executable, str(SHARED_SCRIPTS / "briefing_attribution.py"), *argv],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        check=False,
        timeout=30,
    )


class TestA15FullPath:
    def test_annotated_archive_merges_and_deletes_envelope(self, project):
        archive = project / "briefing.md"
        archive.write_text(
            marked("urgent", "u-old") + ANNOTATION + marked("finance", "f-old"),
            encoding="utf-8",
        )
        before = archive.read_bytes()
        ann = ANNOTATION.encode("utf-8")
        ann_at = before.index(ann)

        env_dir = project / ".cos-tmp"
        env_dir.mkdir()
        env = env_dir / "briefing-sections.json"
        env.write_text(
            json.dumps(
                {"version": 1, "sections": {"urgent": "u-new", "finance": "f-new"}}
            ),
            encoding="utf-8",
        )

        proc = _run_cli(["merge", "--artifact", "briefing", "--sections", str(env)])
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout)
        assert payload["status"] == "merged"

        after = archive.read_bytes()
        assert after.index(ann) == ann_at
        assert after[ann_at : ann_at + len(ann)] == ann
        text = after.decode("utf-8")
        assert text.index(end("urgent")) < text.index(ANNOTATION) < text.index(begin("finance", "f-new"))
        assert "u-old" not in text and "u-new" in text
        assert not env.exists()


class TestA15Reject:
    def test_undeclared_id_exits_2_without_writing(self, project):
        """Undeclared envelope id: exit 2, archive untouched, envelope deleted.

        The shipped helper reports status ``error`` for this validation
        reject (exit 2). Parser refusals use status ``refused``. Envelope
        deletion follows C-9: paths under ``.cos-tmp`` are removed on every
        exit except lock-busy (exit 4) and crash.
        """
        archive = project / "briefing.md"
        archive.write_text(marked("pipeline", "p") + ANNOTATION, encoding="utf-8")
        before = archive.read_bytes()

        env_dir = project / ".cos-tmp"
        env_dir.mkdir()
        env = env_dir / "briefing-sections.json"
        env.write_text(
            json.dumps(
                {
                    "version": 1,
                    "sections": {"pipeline": "p2", "not-in-registry": "x"},
                }
            ),
            encoding="utf-8",
        )

        proc = _run_cli(["merge", "--artifact", "briefing", "--sections", str(env)])
        assert proc.returncode == 2, proc.stderr
        payload = json.loads(proc.stdout)
        assert payload["status"] == "error"
        assert "undeclared" in (payload.get("reason") or "")
        assert archive.read_bytes() == before
        assert not env.exists()


class TestA16Doctor:
    def _check(self, project):
        from doctor_base import _check_briefing_archive

        data = {"paths": {"project_root": str(project)}}
        return _check_briefing_archive(False, data, project / "company.yaml")

    def test_archive_without_log_warns(self, project):
        (project / "briefing.md").write_text("archive\n", encoding="utf-8")
        result = self._check(project)
        assert result.name == "briefing_archive"
        assert result.status == "warn"
        assert result.detail == (
            "briefing archive not merged in the last 36h (or ever) — attribution helper unused"
        )

        (project / ba.LOG_NAME).write_text("", encoding="utf-8")
        again = self._check(project)
        assert again.status == "warn"
        assert again.detail == result.detail

    def test_fresh_log_passes_using_most_recent_ts(self, project):
        (project / "briefing.md").write_text("archive\n", encoding="utf-8")
        fresh = datetime.now(timezone.utc).isoformat()
        stale = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        # Older line is last on disk; the check must follow the timestamp.
        (project / ba.LOG_NAME).write_text(
            json.dumps({"artifact": "briefing", "ts": fresh, "status": "merged"})
            + "\n"
            + json.dumps({"artifact": "briefing", "ts": stale, "status": "merged"})
            + "\n",
            encoding="utf-8",
        )
        result = self._check(project)
        assert result.status == "pass"
        assert result.detail == f"briefing archive last merged: {fresh}"

    def test_log_older_than_36h_warns_when_archive_exists(self, project):
        (project / "briefing.md").write_text("archive\n", encoding="utf-8")
        old = (datetime.now(timezone.utc) - timedelta(hours=37)).isoformat()
        (project / ba.LOG_NAME).write_text(
            json.dumps({"artifact": "briefing", "ts": old, "status": "merged"}) + "\n",
            encoding="utf-8",
        )
        result = self._check(project)
        assert result.status == "warn"
        assert result.detail == f"briefing archive last merged: {old} (>36h ago)"

    def test_stale_success_with_fresh_refused_warns(self, project):
        (project / "briefing.md").write_text("archive\n", encoding="utf-8")
        fresh = datetime.now(timezone.utc).isoformat()
        old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        reason = "operator edit in span"
        (project / ba.LOG_NAME).write_text(
            json.dumps({"artifact": "briefing", "ts": old, "status": "merged"})
            + "\n"
            + json.dumps(
                {
                    "artifact": "briefing",
                    "ts": fresh,
                    "status": "refused",
                    "reason": reason,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        result = self._check(project)
        assert result.status == "warn"
        assert result.detail == (
            f"briefing archive last merged: {old} (>36h ago); "
            f"last attempt: {fresh} refused: {reason}"
        )

    def test_failure_only_log_warns_not_successfully_merged(self, project):
        (project / "briefing.md").write_text("archive\n", encoding="utf-8")
        fresh = datetime.now(timezone.utc).isoformat()
        reason = "undeclared section id"
        (project / ba.LOG_NAME).write_text(
            json.dumps(
                {
                    "artifact": "briefing",
                    "ts": fresh,
                    "status": "refused",
                    "reason": reason,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        result = self._check(project)
        assert result.status == "warn"
        assert result.detail == (
            "briefing archive not successfully merged "
            f"(last attempt: {fresh} refused: {reason})"
        )

    def test_stale_success_with_fresh_error_warns(self, project):
        (project / "briefing.md").write_text("archive\n", encoding="utf-8")
        fresh = datetime.now(timezone.utc).isoformat()
        old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        reason = "backup failed"
        (project / ba.LOG_NAME).write_text(
            json.dumps({"artifact": "briefing", "ts": old, "status": "merged"})
            + "\n"
            + json.dumps(
                {
                    "artifact": "briefing",
                    "ts": fresh,
                    "status": "error",
                    "reason": reason,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        result = self._check(project)
        assert result.status == "warn"
        assert result.detail == (
            f"briefing archive last merged: {old} (>36h ago); "
            f"last attempt: {fresh} error: {reason}"
        )

    def test_naive_timestamp_older_than_36h_warns(self, project):
        (project / "briefing.md").write_text("archive\n", encoding="utf-8")
        naive = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=37)
        ts_raw = naive.isoformat()
        assert "+" not in ts_raw and "Z" not in ts_raw
        (project / ba.LOG_NAME).write_text(
            json.dumps({"artifact": "briefing", "ts": ts_raw, "status": "merged"}) + "\n",
            encoding="utf-8",
        )
        result = self._check(project)
        assert result.status == "warn"
        assert result.detail == f"briefing archive last merged: {ts_raw} (>36h ago)"

    def test_fresh_merged_with_one_malformed_line_passes(self, project):
        (project / "briefing.md").write_text("archive\n", encoding="utf-8")
        fresh = datetime.now(timezone.utc).isoformat()
        (project / ba.LOG_NAME).write_text(
            "{not-json\n"
            + json.dumps({"artifact": "briefing", "ts": fresh, "status": "merged"})
            + "\n",
            encoding="utf-8",
        )
        result = self._check(project)
        assert result.status == "pass"
        assert result.detail == (
            f"briefing archive last merged: {fresh}; 1 unreadable log lines"
        )

    def test_all_malformed_log_warns_unavailable(self, project):
        (project / "briefing.md").write_text("archive\n", encoding="utf-8")
        (project / ba.LOG_NAME).write_text(
            "{bad\n" + json.dumps({"artifact": "briefing", "status": "merged"}) + "\n",
            encoding="utf-8",
        )
        result = self._check(project)
        assert result.status == "warn"
        assert result.detail == "briefing archive unavailable: 2 unreadable log lines"

    def test_no_archive_and_no_log_is_idle(self, project):
        result = self._check(project)
        assert result.status == "pass"
        assert result.detail == "no briefing archive yet (attribution helper idle)"

    def test_config_missing_warns_unavailable(self, project):
        from doctor_base import _check_briefing_archive

        result = _check_briefing_archive(False, None, project / "company.yaml")
        assert result.name == "briefing_archive"
        assert result.status == "warn"
        assert "unavailable:" in result.detail

    def test_registered_between_audit_runs_and_workspace_provider(self):
        import doctor_base

        names = [fn.__name__ for fn in doctor_base.CHECKS]
        assert names.index("_check_briefing_archive") == names.index("_check_audit_runs") + 1
        assert names[names.index("_check_briefing_archive") + 1] == "_check_workspace_provider"


class TestGitignore:
    def test_plugin_gitignore_lists_attribution_runtime_paths(self, tmp_path):
        text = (PLUGIN_ROOT / ".gitignore").read_text(encoding="utf-8")
        lines = text.splitlines()
        assert "# Generated-Section Attribution runtime (data-repo ignore)" in lines
        runtime = (".cos-backups/", ".cos-tmp/", ".cos-briefing.lock")
        for entry in runtime:
            assert entry in lines

        import state_sync

        for entry in runtime:
            assert entry in state_sync.GITIGNORE_ENTRIES
        old = [
            ".env",
            ".env.*",
            "*.db-wal",
            "*.db-shm",
            "*.db-journal",
            "__pycache__/",
            ".runs/",
        ]
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("\n".join(old) + "\n", encoding="utf-8")
        state_sync._ensure_gitignore(tmp_path)
        once = gitignore.read_text(encoding="utf-8").splitlines()
        state_sync._ensure_gitignore(tmp_path)
        twice = gitignore.read_text(encoding="utf-8").splitlines()
        assert once == twice
        assert once[: len(old)] == old
        for entry in runtime:
            assert once.count(entry) == 1
        assert once[-3:] == list(runtime)


class TestSkillAdoption:
    def test_skill_instructs_envelope_merge_after_output_format(self):
        text = (PLUGIN_ROOT / "skills" / "daily-briefing" / "SKILL.md").read_text(encoding="utf-8")
        output_at = text.index("## Output Format")
        archive_at = text.index("## Archive Merge (Generated-Section Attribution)")
        guidance_at = text.index("## Section Guidance")
        assert output_at < archive_at < guidance_at
        section = text[archive_at:guidance_at]
        assert "project_root/.cos-tmp/briefing-sections.json" in section
        assert '"version": 1' in section
        assert (
            ".venv/bin/python shared/scripts/briefing_attribution.py merge "
            "--artifact briefing --sections"
        ) in section
        assert "plugin root" in section
        assert "os.makedirs" in section and "json.dumps" in section
        assert "lock-busy (exit 4)" in section
        assert "leave the envelope in place" in section
        assert "one unchanged retry" in section
        for sid in (
            "urgent",
            "calendar",
            "deadlines",
            "pipeline",
            "finance",
            "todos",
            "inbox-summary",
            "all-clear",
            "pending-high",
            "pending-medium",
            "pending-low",
        ):
            assert f"`{sid}`" in section
        assert "header" in section and "footer" in section
        assert "merged" in section and "noop" in section
        assert "refused" in section and "error" in section
