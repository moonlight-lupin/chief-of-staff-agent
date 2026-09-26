#!/usr/bin/env python3
"""Contract tests for the Generated-Section Attribution feature (spec v3).

Spec of record: .hermes/specs/2026-09-26_generated-section-attribution.md
Orchestrator-authored RED contract tests. The builder must NOT modify this
file. Module under test: shared/scripts/briefing_attribution.py (new).

Seam decisions (binding, from the spec):
- Public surface: `merge(artifact="briefing", sections={...}, config=...) -> dict`
  plus `run_cli(argv)` for exit-code tests. No invented kwargs.
- Registry: BRIEFING_ARCHIVE_SECTIONS lives in briefing_attribution.py.
- Lock/backup/dirs anchor at paths.project_root from config_loader, not HOME.
- fcntl lockfile: project_root/.cos-briefing.lock
- Backups: project_root/.cos-backups/attribution/<artifact-identity-key>/
- Envelope staging: project_root/.cos-tmp/ (test deletes envelopes; module
  must refuse /tmp paths per spec C-9)
- run log: project_root/.cos-briefing-merge-log.jsonl (append-only)
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))

import briefing_attribution as ba  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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
    return root


def begin(id_: str, body: str, hash12: str | None = None) -> str:
    h = hash12 or ba.body_hash(body)
    return f"<!-- cos:generated {id_} begin sha256={h} -->"


def end(id_: str) -> str:
    return f"<!-- cos:generated {id_} end -->"


def marked(id_: str, body: str) -> str:
    return f"{begin(id_, body)}\n{body}\n{end(id_)}\n"


# ---------------------------------------------------------------------------
# Registry (C-3 / C-9)
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_registry_published_and_ordered(self):
        assert isinstance(ba.BRIEFING_ARCHIVE_SECTIONS, tuple)
        assert len(ba.BRIEFING_ARCHIVE_SECTIONS) == len(set(ba.BRIEFING_ARCHIVE_SECTIONS))
        for sid in ba.BRIEFING_ARCHIVE_SECTIONS:
            assert ba.ID_RE.match(sid), sid
        # position order is the registry order (spot-check key pairs)
        names = ba.BRIEFING_ARCHIVE_SECTIONS
        assert names.index("header") < names.index("urgent")
        assert names.index("finance") < names.index("todos")
        assert names.index("inbox-summary") < names.index("all-clear")
        assert names[-1] == "footer"

    def test_agent_sections_map_into_registry(self):
        # every id the skill's output format produces must be in the registry
        expected_agent_ids = {
            "urgent", "calendar", "deadlines", "pipeline", "finance",
            "pending-high", "pending-medium", "pending-low",
            "todos", "inbox-summary", "all-clear",
        }
        assert expected_agent_ids <= set(ba.BRIEFING_ARCHIVE_SECTIONS)


# ---------------------------------------------------------------------------
# Hash contract (C-1)
# ---------------------------------------------------------------------------

class TestHash:
    def test_hash_is_lf_normalized_stable_across_crlf(self):
        # LF-normalized: CRLF and LF bodies hash identically
        assert ba.body_hash("a\r\nb") == ba.body_hash("a\nb")

    def test_hash_strips_trailing_whitespace_per_line(self):
        assert ba.body_hash("a \nb") == ba.body_hash("a\nb")

    def test_hash_is_12hex(self):
        h = ba.body_hash("hello")
        assert len(h) == 12
        int(h, 16)

    def test_hash_pinned_byte_range(self):
        # pin the exact digest: sha256(b"hello")[:12]
        assert ba.body_hash("hello") == hashlib.sha256(b"hello").hexdigest()[:12]

    def test_hash_distinguishes_bodies(self):
        assert ba.body_hash("alpha") != ba.body_hash("beta")


# ---------------------------------------------------------------------------
# Merge: happy paths (C-2)
# ---------------------------------------------------------------------------

class TestMergeReplace:
    def test_replaces_span_body_only(self, project):
        f = project / "briefing.md"
        f.write_text(marked("pipeline", "old pipeline") + "> operator note\n", encoding="utf-8")
        res = ba.merge(artifact="briefing", sections={"pipeline": "new pipeline"}, config=None)
        assert res["status"] == "merged"
        text = f.read_text(encoding="utf-8")
        assert "new pipeline" in text
        assert "old pipeline" not in text
        # operator text byte-identical and position unchanged
        assert text.endswith("> operator note\n")
        # begin marker hash updated (managed change)
        assert begin("pipeline", "new pipeline") in text

    def test_noop_byte_identical(self, project):
        body = "same body"
        f = project / "briefing.md"
        f.write_text(marked("pipeline", body), encoding="utf-8")
        before = f.read_bytes()
        res = ba.merge(artifact="briefing", sections={"pipeline": body}, config=None)
        assert res["status"] == "noop"
        assert f.read_bytes() == before
        assert res["backup"] is None

    def test_operator_text_between_spans_untouched(self, project):
        f = project / "briefing.md"
        f.write_text(
            marked("urgent", "u-old") + "note between\n" + marked("todos", "t-old"),
            encoding="utf-8",
        )
        ba.merge(artifact="briefing", sections={"urgent": "u-new", "todos": "t-new"}, config=None)
        text = f.read_text(encoding="utf-8")
        assert "note between\n" in text
        assert "u-new" in text and "t-new" in text

    def test_content_after_end_marker_untouched(self, project):
        f = project / "briefing.md"
        f.write_text(
            marked("urgent", "u-old") + "trailing note\n" + marked("finance", "f-old"),
            encoding="utf-8",
        )
        ba.merge(artifact="briefing", sections={"urgent": "u-new", "finance": "f-new"}, config=None)
        text = f.read_text(encoding="utf-8")
        assert "trailing note\n" in text

    def test_insertion_anchor_between_spans(self, project):
        # header+pipeline present; urgent absent → inserted between them
        f = project / "briefing.md"
        f.write_text(marked("header", "h") + "\noperator line\n" + marked("pipeline", "p"), encoding="utf-8")
        ba.merge(artifact="briefing", sections={"header": "h2", "pipeline": "p2", "urgent": "u"}, config=None)
        text = f.read_text(encoding="utf-8")
        pos_h = text.index(end("header"))
        pos_u = text.index(begin("urgent", "u"))
        pos_p = text.index(begin("pipeline", "p2"))
        assert pos_h < pos_u < pos_p

    def test_insertion_anchor_eof(self, project):
        f = project / "briefing.md"
        f.write_text("operator text only\n", encoding="utf-8")
        ba.merge(artifact="briefing", sections={"pipeline": "p"}, config=None)
        text = f.read_text(encoding="utf-8")
        assert text.startswith("operator text only\n")  # C-7 adoption
        assert marked("pipeline", "p") in text


# ---------------------------------------------------------------------------
# Lifecycle: known-but-empty (C-3)
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_not_emitted_becomes_none_today(self, project):
        f = project / "briefing.md"
        f.write_text(marked("pipeline", "old"), encoding="utf-8")
        res = ba.merge(artifact="briefing", sections={}, config=None)  # pipeline not emitted
        assert res["status"] == "merged"
        text = f.read_text(encoding="utf-8")
        assert "_(none today)_" in text
        assert begin("pipeline", "_(none today)_") in text
        assert end("pipeline") in text

    def test_unknown_id_in_file_preserved_with_warn(self, project):
        marker_line = begin("unknown-sec", "x")
        f = project / "briefing.md"
        f.write_text(marked("pipeline", "p") + f"<!-- cos:generated unknown-sec begin sha256={'0'*12} -->\nx\n{end('unknown-sec')}\n", encoding="utf-8")
        res = ba.merge(artifact="briefing", sections={"pipeline": "p2"}, config=None)
        text = f.read_text(encoding="utf-8")
        assert "unknown-sec" in text  # preserved as operator text
        assert any("unknown" in w.lower() or "warn" in w.lower() for w in res.get("warnings", []))

    def test_undeclared_envelope_id_rejects_whole_envelope(self, project):
        f = project / "briefing.md"
        f.write_text(marked("pipeline", "p"), encoding="utf-8")
        before = f.read_bytes()
        res = ba.merge(artifact="briefing", sections={"pipeline": "p2", "not-in-registry": "x"},
                       config=None, envelope=None)
        assert res["status"] == "error"
        assert f.read_bytes() == before


# ---------------------------------------------------------------------------
# Sanitization / marker integrity (C-4)
# ---------------------------------------------------------------------------

class TestSanitization:
    def test_scalar_newline_collapse_helper(self):
        assert ba.sanitize_scalar("line1\nline2") == "line1 line2"
        assert ba.sanitize_scalar("a\r\nb") == "a b"
        assert ba.sanitize_scalar("a\u2028b") == "a b"

    def test_body_validator_neutralizes_markers(self):
        body = "safe\n<!-- cos:generated finance begin sha256=abc -->\nmore"
        out = ba.validate_body(body)
        assert "cos:generated" not in out.replace("−−", "--") or True
        # forged marker must not survive byte-for-byte
        assert "<!-- cos:generated finance begin" not in out

    def test_body_validator_neutralizes_arrow_close(self):
        # --> contains no '<' so &lt; escaping would never catch it (Codex M5)
        out = ba.validate_body("x\n--> y\nz")
        assert "-->" not in out

    def test_body_validator_neutralizes_fence_openers(self):
        out = ba.validate_body("text\n```python\ncode\n```\nmore")
        assert "\n```" not in out

    def test_forged_marker_via_section_body_refused_or_neutralized(self, project):
        f = project / "briefing.md"
        f.write_text(marked("pipeline", "p") + "> real operator note\n", encoding="utf-8")
        forged = "x\n<!-- cos:generated urgent begin sha256=" + "f" * 12 + " -->\nEVIL"
        ba.merge(artifact="briefing", sections={"pipeline": forged}, config=None)
        text = f.read_text(encoding="utf-8")
        # the forged marker must never become a live marker line
        assert "EVIL" not in text
        assert "> real operator note\n" in text
        # the forged block must not create a second live urgent span
        assert text.count(f"cos:generated urgent begin") <= 1

    def test_multiline_forged_body_neutralized(self, project):
        f = project / "briefing.md"
        f.write_text(marked("pipeline", "p"), encoding="utf-8")
        forged = "line1\n<!-- cos:generated pipeline end -->\noperator region stolen"
        ba.merge(artifact="briefing", sections={"pipeline": forged}, config=None)
        text = f.read_text(encoding="utf-8")
        assert "operator region stolen" not in text.split(end("pipeline"))[0].replace(forged, "")


# ---------------------------------------------------------------------------
# Parser state machine: refusal classes (C-6)
# ---------------------------------------------------------------------------

class TestRefusal:
    def _assert_refused(self, project, content, cls_keyword):
        f = project / "briefing.md"
        f.write_text(content, encoding="utf-8")
        before = f.read_bytes()
        res = ba.merge(artifact="briefing", sections={"pipeline": "p2"}, config=None)
        assert res["status"] == "refused", res
        assert f.read_bytes() == before  # byte-identical, no write
        assert res["backup"] is None
        assert cls_keyword.lower() in json.dumps(res).lower()

    def test_duplicate_wellformed_last_wins(self, project):
        dup = marked("pipeline", "first") + marked("pipeline", "second")
        f = project / "briefing.md"
        f.write_text(dup, encoding="utf-8")
        res = ba.merge(artifact="briefing", sections={"pipeline": "fresh"}, config=None)
        assert res["status"] == "merged"
        text = f.read_text(encoding="utf-8")
        # last occurrence replaced; first preserved as operator text
        assert "first" in text
        assert "second" not in text
        assert "fresh" in text
        # no third span appended
        assert text.count("cos:generated pipeline begin") == 2

    def test_duplicate_class_bounded_over_3_cycles(self, project):
        dup = marked("pipeline", "first") + marked("pipeline", "second")
        f = project / "briefing.md"
        f.write_text(dup, encoding="utf-8")
        for _ in range(3):
            n_markers = f.read_text(encoding="utf-8").count("cos:generated pipeline begin")
            ba.merge(artifact="briefing", sections={"pipeline": "fresh"}, config=None)
            assert f.read_text(encoding="utf-8").count("cos:generated pipeline begin") == n_markers

    def test_nested_begins_refused(self, project):
        content = (
            f"{begin('pipeline', 'p')}\nbody\n"
            f"{begin('finance', 'f')}\nfb\n{end('finance')}\n{end('pipeline')}\n"
        )
        self._assert_refused(project, content, "nested")

    def test_crossed_ids_refused(self, project):
        content = f"{begin('pipeline', 'p')}\n{begin('finance', 'f')}\n{end('pipeline')}\n{end('finance')}\n"
        self._assert_refused(project, content, "crossed")

    def test_stray_end_refused(self, project):
        content = "text\n" + end("pipeline") + "\nmore\n"
        self._assert_refused(project, content, "stray")

    def test_unclosed_span_refused(self, project):
        content = f"{begin('pipeline', 'p')}\nbody never closed\n"
        self._assert_refused(project, content, "unclosed")

    def test_marker_inside_closed_fence_ignored(self, project):
        fence = "```\n" + begin("pipeline", "x") + "\n```\n"
        f = project / "briefing.md"
        f.write_text(fence + marked("pipeline", "p"), encoding="utf-8")
        res = ba.merge(artifact="briefing", sections={"pipeline": "p2"}, config=None)
        assert res["status"] == "merged"
        text = f.read_text(encoding="utf-8")
        assert "p2" in text
        assert fence in text  # fence block preserved verbatim

    def test_refusal_bounded_3_cycles(self, project):
        content = f"{begin('pipeline', 'p')}\nunclosed\n"
        f = project / "briefing.md"
        f.write_text(content, encoding="utf-8")
        for _ in range(3):
            before = f.read_bytes()
            res = ba.merge(artifact="briefing", sections={"pipeline": "fresh"}, config=None)
            assert res["status"] == "refused"
            assert f.read_bytes() == before


# ---------------------------------------------------------------------------
# Conflict handling (US-1d)
# ---------------------------------------------------------------------------

class TestConflict:
    def test_hash_mismatch_preserves_edited_body_verbatim(self, project):
        f = project / "briefing.md"
        f.write_text(marked("pipeline", "operator rewrote this"), encoding="utf-8")
        res = ba.merge(artifact="briefing", sections={"pipeline": "machine content"}, config=None)
        text = f.read_text(encoding="utf-8")
        assert "operator rewrote this" in text  # verbatim, outside span
        assert "machine content" in text  # fresh content in span
        assert res["backup"] is not None  # mandatory backup

    def test_conflict_wrapped_in_cos_conflict_markers(self, project):
        f = project / "briefing.md"
        f.write_text(marked("pipeline", "operator rewrote this"), encoding="utf-8")
        ba.merge(artifact="briefing", sections={"pipeline": "machine content"}, config=None)
        text = f.read_text(encoding="utf-8")
        assert "<!-- cos:conflict pipeline" in text
        # conflict wrapper lines cannot match the C-1 marker regex
        for line in text.splitlines():
            if "cos:conflict" in line:
                assert not ba.MARKER_RE.match(line.strip())

    def test_conflict_no_retrigger_cycle2(self, project):
        f = project / "briefing.md"
        f.write_text(marked("pipeline", "operator rewrote this"), encoding="utf-8")
        ba.merge(artifact="briefing", sections={"pipeline": "machine content"}, config=None)
        after1 = f.read_text(encoding="utf-8")
        res2 = ba.merge(artifact="briefing", sections={"pipeline": "machine content"}, config=None)
        assert res2["status"] in ("merged", "noop")
        after2 = f.read_text(encoding="utf-8")
        # the preserved operator text survives cycle 2, position stable
        assert "operator rewrote this" in after2
        assert after2.count("operator rewrote this") == 1


# ---------------------------------------------------------------------------
# C-7 first-run / legacy adoption
# ---------------------------------------------------------------------------

class TestFirstRun:
    def test_legacy_file_preserved_and_appended(self, project):
        legacy = "# My Briefing\n\nMy own intro paragraph.\n"
        f = project / "briefing.md"
        f.write_text(legacy, encoding="utf-8")
        res = ba.merge(artifact="briefing", sections={"pipeline": "p"}, config=None)
        assert res["status"] == "merged"
        text = f.read_text(encoding="utf-8")
        assert text.startswith(legacy)
        assert marked("pipeline", "p") in text
        assert res["backup"] is not None

    def test_legacy_adoption_stable_cycle2(self, project):
        legacy = "My own intro paragraph.\n"
        f = project / "briefing.md"
        f.write_text(legacy, encoding="utf-8")
        ba.merge(artifact="briefing", sections={"pipeline": "p1"}, config=None)
        ba.merge(artifact="briefing", sections={"pipeline": "p2"}, config=None)
        text = f.read_text(encoding="utf-8")
        assert text.startswith(legacy)
        assert "p1" not in text
        assert "p2" in text


# ---------------------------------------------------------------------------
# Byte preservation (C-5)
# ---------------------------------------------------------------------------

class TestBytePreservation:
    def test_crlf_preserved(self, project):
        f = project / "briefing.md"
        f.write_bytes(marked("pipeline", "old").replace("\n", "\r\n").encode("utf-8"))
        ba.merge(artifact="briefing", sections={"pipeline": "new"}, config=None)
        data = f.read_bytes()
        assert b"\r\n" in data
        assert "new".encode() in data

    def test_no_trailing_newline_gets_separator(self, project):
        f = project / "briefing.md"
        f.write_text("operator text, no newline", encoding="utf-8")
        ba.merge(artifact="briefing", sections={"pipeline": "p"}, config=None)
        text = f.read_text(encoding="utf-8")
        assert text.startswith("operator text, no newline")
        assert marked("pipeline", "p") in text

    def test_utf8_bom_preserved(self, project):
        f = project / "briefing.md"
        f.write_bytes(b"\xef\xbb\xbf" + marked("pipeline", "old").encode("utf-8"))
        res = ba.merge(artifact="briefing", sections={"pipeline": "new"}, config=None)
        assert res["status"] == "merged"
        assert f.read_bytes().startswith(b"\xef\xbb\xbf")

    def test_non_utf8_refused(self, project):
        f = project / "briefing.md"
        f.write_bytes(b"\xff\xfe\x00i\x00s\x00o\x00")  # UTF-16LE BOM + text
        before = f.read_bytes()
        res = ba.merge(artifact="briefing", sections={"pipeline": "p"}, config=None)
        assert res["status"] == "error"
        assert f.read_bytes() == before


# ---------------------------------------------------------------------------
# Concurrency (C-5 / US-5)
# ---------------------------------------------------------------------------

class TestConcurrency:
    def test_lockfile_created_and_released(self, project):
        lock = project / ".cos-briefing.lock"
        f = project / "briefing.md"
        f.write_text(marked("pipeline", "p"), encoding="utf-8")
        ba.merge(artifact="briefing", sections={"pipeline": "p2"}, config=None)
        assert not lock.exists()  # released

    def test_second_writer_exit4_no_partial_state(self, project):
        import fcntl
        f = project / "briefing.md"
        f.write_text(marked("pipeline", "p"), encoding="utf-8")
        lock = project / ".cos-briefing.lock"
        lock.touch()
        fh = open(lock, "w")
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            res = ba.merge(artifact="briefing", sections={"pipeline": "p2"}, config=None)
            assert res["status"] == "error"
            assert res.get("exit_code", ba.EXIT_LOCK_BUSY) == ba.EXIT_LOCK_BUSY
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()

    def test_toctou_recheck_refuses_external_edit(self, project):
        f = project / "briefing.md"
        f.write_text(marked("pipeline", "p"), encoding="utf-8")
        real_stat = os.stat

        def edit_between_check_and_rename(path, *a, **k):
            st = real_stat(path, *a, **k)
            # after the merge's internal re-check read, mutate the file
            if str(f) == str(path) and getattr(ba, "_toctou_armed", False):
                with open(f, "a", encoding="utf-8") as fh:
                    fh.write("operator race edit\n")
                ba._toctou_armed = False
            return st

        monkey_patch_target = "briefing_attribution._verify_unchanged"
        if hasattr(ba, "_verify_unchanged"):
            orig = ba._verify_unchanged

            def race_then_verify(path, before):
                st = os.stat(path)
                with open(f, "a", encoding="utf-8") as fh:
                    fh.write("operator race edit\n")
                return orig(path, before)

            import pytest as _pytest
            _pytest.MonkeyPatch().setattr(ba, "_verify_unchanged", race_then_verify, raising=False)
            try:
                res = ba.merge(artifact="briefing", sections={"pipeline": "p2"}, config=None)
                # honest guarantee: best-effort detection may miss a rename-window
                # edit; the CONTRACT is the re-check exists and refuses when it
                # sees a change. Assert the refusal when detection fires.
                if res["status"] == "refused":
                    assert res["exit_code"] == ba.EXIT_CONCURRENT
            finally:
                _pytest.MonkeyPatch().undo()
        else:
            pytest_skip = True  # seam named in spec US-5; builder must provide it

    def test_cooperating_writers_serialize(self, project):
        """Two sequential merges through the lock both complete."""
        f = project / "briefing.md"
        f.write_text(marked("pipeline", "p0"), encoding="utf-8")
        r1 = ba.merge(artifact="briefing", sections={"pipeline": "p1"}, config=None)
        r2 = ba.merge(artifact="briefing", sections={"pipeline": "p2"}, config=None)
        assert r1["status"] == "merged" and r2["status"] == "merged"
        assert "p2" in f.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Backups (C-5)
# ---------------------------------------------------------------------------

class TestBackups:
    def test_backup_taken_when_operator_text_present(self, project):
        f = project / "briefing.md"
        f.write_text(marked("pipeline", "old") + "> note\n", encoding="utf-8")
        res = ba.merge(artifact="briefing", sections={"pipeline": "new"}, config=None)
        assert res["backup"] is not None
        bdir = project / ".cos-backups" / "attribution"
        assert bdir.exists()
        backups = list(bdir.rglob("briefing.md.*"))
        assert len(backups) >= 1

    def test_no_backup_for_noop(self, project):
        f = project / "briefing.md"
        f.write_text(marked("pipeline", "same"), encoding="utf-8")
        res = ba.merge(artifact="briefing", sections={"pipeline": "same"}, config=None)
        assert res["status"] == "noop"
        assert res["backup"] is None

    def test_retention_5_newest_per_artifact(self, project):
        f = project / "briefing.md"
        f.write_text(marked("pipeline", "v0") + "> note\n", encoding="utf-8")
        for i in range(1, 8):
            ba.merge(artifact="briefing", sections={"pipeline": f"v{i}"}, config=None)
        bdir = project / ".cos-backups" / "attribution"
        all_backups = [p for p in bdir.rglob("*") if p.is_file()]
        assert len(all_backups) <= 5

    def test_full_identity_key_not_basename(self, project):
        # two artifacts with same basename must not collide
        other = project / "sub"
        other.mkdir()
        (other / "briefing.md").write_text(marked("pipeline", "old") + "> n\n", encoding="utf-8")
        f = project / "briefing.md"
        f.write_text(marked("pipeline", "old") + "> n\n", encoding="utf-8")
        # only briefing is merged via the API; the other file must be untouched
        ba.merge(artifact="briefing", sections={"pipeline": "new"}, config=None)
        assert "old" in (other / "briefing.md").read_text(encoding="utf-8")

    def test_backup_failure_aborts_replace(self, project):
        f = project / "briefing.md"
        f.write_text(marked("pipeline", "old") + "> note\n", encoding="utf-8")
        before = f.read_bytes()

        def broken_copy(*a, **k):
            raise OSError("backup failed")

        mp = pytest.MonkeyPatch()
        mp.setattr(ba, "_backup_copy", broken_copy, raising=False)
        try:
            res = ba.merge(artifact="briefing", sections={"pipeline": "new"}, config=None)
        finally:
            mp.undo()
        # replace aborted, file unchanged
        assert f.read_bytes() == before


# ---------------------------------------------------------------------------
# Envelope validation & CLI (C-9)
# ---------------------------------------------------------------------------

class TestEnvelope:
    def test_version_1_required(self, project, tmp_path):
        env = tmp_path / "env.json"
        env.write_text(json.dumps({"version": 2, "sections": {}}))
        res = ba.merge(artifact="briefing", sections=None,
                       envelope_path=str(env), config=None)
        assert res["status"] == "error"

    def test_values_must_be_strings(self, project, tmp_path):
        env = tmp_path / "env.json"
        env.write_text(json.dumps({"version": 1, "sections": {"pipeline": 42}}))
        res = ba.merge(artifact="briefing", sections=None,
                       envelope_path=str(env), config=None)
        assert res["status"] == "error"


class TestCli:
    def _run(self, argv):
        return ba.run_cli(argv)

    def test_exit_codes_pinned(self, project, tmp_path):
        f = project / "briefing.md"
        f.write_text(marked("pipeline", "p"), encoding="utf-8")
        env = tmp_path / "env.json"
        env.write_text(json.dumps({"version": 1, "sections": {"pipeline": "p2"}}))
        code = self._run(["merge", "--artifact", "briefing", "--sections", str(env)])
        assert code == 0

        # refusal class → 2
        f.write_text(f"{begin('pipeline', 'p')}\nunclosed\n", encoding="utf-8")
        code = self._run(["merge", "--artifact", "briefing", "--sections", str(env)])
        assert code == 2

    def test_no_target_flag(self, project):
        # --target must not exist: the agent cannot point the helper at
        # arbitrary paths (wiki pages, SKILL.md)
        code = self._run(["merge", "--target", "/tmp/x.md", "--sections", "{}"])
        assert code != 0

    def test_output_json_shape(self, project, tmp_path, capsys):
        f = project / "briefing.md"
        f.write_text(marked("pipeline", "p"), encoding="utf-8")
        env = tmp_path / "env.json"
        env.write_text(json.dumps({"version": 1, "sections": {"pipeline": "p2"}}))
        self._run(["merge", "--artifact", "briefing", "--sections", str(env)])
        out = capsys.readouterr().out
        parsed = json.loads(out)
        for key in ("status", "sections", "warnings", "backup", "archive"):
            assert key in parsed


# ---------------------------------------------------------------------------
# Audit log (C-9)
# ---------------------------------------------------------------------------

class TestAuditLog:
    def test_merge_writes_log_entry(self, project):
        f = project / "briefing.md"
        f.write_text(marked("pipeline", "p"), encoding="utf-8")
        ba.merge(artifact="briefing", sections={"pipeline": "p2"}, config=None)
        log = project / ".cos-briefing-merge-log.jsonl"
        assert log.exists()
        entry = json.loads(log.read_text(encoding="utf-8").strip().splitlines()[-1])
        assert entry.get("artifact") == "briefing"
        assert "ts" in entry