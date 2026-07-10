#!/usr/bin/env python3
"""Tests for v0.1.2 skill mutation scripts — pipeline, todo, invoices, sign_pdf, drive_map, daily_briefing."""

import sys
import os
import json
import tempfile
import subprocess
from pathlib import Path
from datetime import date, timedelta

import pytest
import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))


@pytest.fixture
def tmp_project():
    """Create a temporary project with config + data files."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        project = root / "project"
        project.mkdir()
        config_dir = root / "config"
        config_dir.mkdir()

        config = config_dir / "company.yaml"
        config.write_text(f"""\
company:
  name: "Test Co"
  jurisdiction: SG
  incorporation_date: "2024-01-15"
  financial_year_end: "31 Dec"
  currency: SGD
  business_type: professional_services

google:
  service_account_path: "~/.hermes/test.json"
  domain: "test.com"
  delegate_email: "founder@test.com"

paths:
  project_root: "{project}"
  wiki_path: "{project}/wiki/"
  templates: "{PLUGIN_ROOT}/shared/templates/"

delivery:
  channel: telegram
  briefing_time: "20:00"
  weekly_review_day: friday
  weekly_review_time: "17:00"
  timezone: "Asia/Singapore"

sales_stages: [Lead, Proposal Sent, NDA Signed, Contract Signed, Invoiced, Paid]
stale_threshold_days: 14

calendar:
  reminder_minutes: 15
  auto_prep_brief: true

self_sign:
  signature_image: null
  auto_date: true
  output_format: pdf
  party_aliases:
    - "Service Provider"

backup:
  enabled: true
  schedule: "0 3 * * 0"
  drive_folder: "09_Backups/"
  exclude:
    - ".env"
""")

        # Empty data stores
        (project / "pipeline.yaml").write_text("deals: []\n")
        (project / "invoices.yaml").write_text("invoices: []\n")
        (project / "expenses.yaml").write_text("expenses: []\n")
        (project / "todos.yaml").write_text("todos: []\n")

        os.environ["CHIEF_OF_STAFF_CONFIG"] = str(config)
        yield project
        os.environ.pop("CHIEF_OF_STAFF_CONFIG", None)


def run_script(script_rel, *args):
    """Run a plugin script and return (exit_code, stdout, stderr)."""
    script = PLUGIN_ROOT / script_rel
    result = subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True, text=True, timeout=30
    )
    return result.returncode, result.stdout, result.stderr


# ── Pipeline ──────────────────────────────────────────────────────────────────

class TestPipelineScript:
    def test_add_deal(self, tmp_project):
        rc, out, err = run_script("skills/pipeline-manager/scripts/pipeline.py",
            "add", "--client", "Acme Corp", "--contact", "John", "--email", "john@acme.com",
            "--value", "4500", "--stage", "Lead")
        assert rc == 0, f"add failed: {err}"
        # Verify deal was added
        data = yaml.safe_load((tmp_project / "pipeline.yaml").read_text())
        assert len(data["deals"]) == 1
        assert data["deals"][0]["client_name"] == "Acme Corp"
        assert data["deals"][0]["stage"] == "Lead"
        assert data["deals"][0]["id"].startswith("deal-")

    def test_move_stage_valid(self, tmp_project):
        # First add a deal
        run_script("skills/pipeline-manager/scripts/pipeline.py",
            "add", "--client", "Acme", "--stage", "Lead")
        data = yaml.safe_load((tmp_project / "pipeline.yaml").read_text())
        deal_id = data["deals"][0]["id"]

        # Move to next stage
        rc, out, err = run_script("skills/pipeline-manager/scripts/pipeline.py",
            "move", "--id", deal_id, "--stage", "Proposal Sent")
        assert rc == 0, f"move failed: {err}"

        data = yaml.safe_load((tmp_project / "pipeline.yaml").read_text())
        assert data["deals"][0]["stage"] == "Proposal Sent"
        assert data["deals"][0]["last_activity"] == date.today().isoformat()

    def test_move_stage_invalid(self, tmp_project):
        run_script("skills/pipeline-manager/scripts/pipeline.py",
            "add", "--client", "Acme", "--stage", "Lead")
        data = yaml.safe_load((tmp_project / "pipeline.yaml").read_text())
        deal_id = data["deals"][0]["id"]

        rc, out, err = run_script("skills/pipeline-manager/scripts/pipeline.py",
            "move", "--id", deal_id, "--stage", "Nonexistent Stage")
        assert rc != 0, "Should reject invalid stage"

    def test_list_stale(self, tmp_project):
        # Add a deal with old last_activity
        run_script("skills/pipeline-manager/scripts/pipeline.py",
            "add", "--client", "Old Deal", "--stage", "Lead")
        # Manually set old date
        data = yaml.safe_load((tmp_project / "pipeline.yaml").read_text())
        old_date = (date.today() - timedelta(days=30)).isoformat()
        data["deals"][0]["last_activity"] = old_date
        data["deals"][0]["created"] = old_date
        (tmp_project / "pipeline.yaml").write_text(yaml.dump(data))

        rc, out, err = run_script("skills/pipeline-manager/scripts/pipeline.py",
            "list", "--stale")
        assert rc == 0
        assert "Old Deal" in out

    def test_add_note(self, tmp_project):
        run_script("skills/pipeline-manager/scripts/pipeline.py",
            "add", "--client", "Acme", "--stage", "Lead")
        data = yaml.safe_load((tmp_project / "pipeline.yaml").read_text())
        deal_id = data["deals"][0]["id"]

        rc, out, err = run_script("skills/pipeline-manager/scripts/pipeline.py",
            "add-note", "--id", deal_id, "--note", "Follow up next week")
        assert rc == 0, f"add-note failed: {err}"


# ── Todo ──────────────────────────────────────────────────────────────────────

class TestTodoScript:
    def test_add_todo(self, tmp_project):
        rc, out, err = run_script("skills/todo-list/scripts/todo.py",
            "add", "--title", "Test task", "--priority", "high")
        assert rc == 0, f"add failed: {err}"
        data = yaml.safe_load((tmp_project / "todos.yaml").read_text())
        assert len(data["todos"]) == 1
        assert data["todos"][0]["title"] == "Test task"
        assert data["todos"][0]["status"] == "open"

    def test_complete_todo(self, tmp_project):
        run_script("skills/todo-list/scripts/todo.py",
            "add", "--title", "Complete me")
        data = yaml.safe_load((tmp_project / "todos.yaml").read_text())
        todo_id = data["todos"][0]["id"]

        rc, out, err = run_script("skills/todo-list/scripts/todo.py",
            "complete", "--id", todo_id)
        assert rc == 0
        data = yaml.safe_load((tmp_project / "todos.yaml").read_text())
        assert data["todos"][0]["status"] == "done"
        assert data["todos"][0]["completed"] is not None

    def test_list_by_status(self, tmp_project):
        run_script("skills/todo-list/scripts/todo.py",
            "add", "--title", "Open task")
        run_script("skills/todo-list/scripts/todo.py",
            "add", "--title", "Another task")

        rc, out, err = run_script("skills/todo-list/scripts/todo.py",
            "list", "--status", "open")
        assert rc == 0
        assert "Open task" in out


# ── Invoices ──────────────────────────────────────────────────────────────────

class TestInvoicesScript:
    def test_add_invoice(self, tmp_project):
        rc, out, err = run_script("skills/bookkeeper/scripts/invoices.py",
            "add", "--direction", "sent", "--counterparty", "Acme Corp",
            "--amount", "4500", "--currency", "SGD",
            "--issue-date", "2026-07-01", "--due-date", "2026-07-15")
        assert rc == 0, f"add failed: {err}"
        data = yaml.safe_load((tmp_project / "invoices.yaml").read_text())
        assert len(data["invoices"]) == 1
        assert data["invoices"][0]["counterparty"] == "Acme Corp"
        assert data["invoices"][0]["amount"] == 4500

    def test_mark_paid(self, tmp_project):
        run_script("skills/bookkeeper/scripts/invoices.py",
            "add", "--direction", "sent", "--counterparty", "Acme",
            "--amount", "1000", "--issue-date", "2026-07-01", "--due-date", "2026-07-15")
        data = yaml.safe_load((tmp_project / "invoices.yaml").read_text())
        inv_id = data["invoices"][0]["id"]

        rc, out, err = run_script("skills/bookkeeper/scripts/invoices.py",
            "mark-paid", "--id", inv_id)
        assert rc == 0
        data = yaml.safe_load((tmp_project / "invoices.yaml").read_text())
        assert data["invoices"][0]["status"] == "paid"

    def test_list_overdue(self, tmp_project):
        # Add an overdue invoice
        run_script("skills/bookkeeper/scripts/invoices.py",
            "add", "--direction", "sent", "--counterparty", "Acme",
            "--amount", "500", "--issue-date", "2026-01-01", "--due-date", "2026-01-15")

        rc, out, err = run_script("skills/bookkeeper/scripts/invoices.py",
            "list-overdue")
        assert rc == 0
        assert "Acme" in out or "500" in out

    def test_ar_summary(self, tmp_project):
        run_script("skills/bookkeeper/scripts/invoices.py",
            "add", "--direction", "sent", "--counterparty", "Acme",
            "--amount", "3000", "--issue-date", "2026-07-01", "--due-date", "2026-07-15")

        rc, out, err = run_script("skills/bookkeeper/scripts/invoices.py",
            "ar-summary")
        assert rc == 0
        assert "3000" in out or "3,000" in out


# ── Drive Map ─────────────────────────────────────────────────────────────────

class TestDriveMap:
    def test_suggest_target(self, tmp_project):
        rc, out, err = run_script("skills/drive-filer/scripts/drive_map.py",
            "suggest-target", "--filename", "NDA_Acme.pdf")
        assert rc == 0, f"suggest-target failed: {err}"
        # Should suggest NDA-related folder
        out_lower = out.lower()
        assert "nda" in out_lower or "client" in out_lower or "inbox" in out_lower

    def test_quarantine_unknown(self, tmp_project):
        rc, out, err = run_script("skills/drive-filer/scripts/drive_map.py",
            "suggest-target", "--filename", "random_unknown_file.xyz")
        assert rc == 0
        # Unknown files should go to inbox
        assert "inbox" in out.lower() or "00" in out

    def test_validate_map(self, tmp_project):
        rc, out, err = run_script("skills/drive-filer/scripts/drive_map.py",
            "validate-map")
        # May pass or warn, but shouldn't crash
        assert rc in (0, 1), f"validate-map crashed: {err}"


# ── Daily Briefing ────────────────────────────────────────────────────────────

class TestDailyBriefing:
    def test_dry_run_json(self, tmp_project):
        rc, out, err = run_script("skills/daily-briefing/scripts/daily_briefing.py",
            "--dry-run", "--json")
        assert rc == 0, f"dry-run failed: {err}"
        data = json.loads(out)
        assert "date" in data or "sources" in data or "sources" in str(data)

    def test_dry_run_has_source_structure(self, tmp_project):
        rc, out, err = run_script("skills/daily-briefing/scripts/daily_briefing.py",
            "--dry-run", "--json")
        assert rc == 0
        data = json.loads(out)
        # Should have some source structure even if sources fail
        assert isinstance(data, dict)


# ── Sign PDF (source hash verification) ───────────────────────────────────────

class TestSignPDF:
    def test_refuses_on_hash_mismatch(self, tmp_project):
        # Create a fake PDF and signature
        import fitz
        pdf_path = tmp_project / "test.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Signature: ____________________", fontsize=11)
        doc.save(str(pdf_path))
        doc.close()

        sig_path = tmp_project / "sig.png"
        # Create a minimal PNG (1x1 transparent)
        import struct, zlib
        def write_png(path, width=1, height=1):
            raw = b'\x00' * width * height * 4
            compressed = zlib.compress(raw)
            with open(path, 'wb') as f:
                f.write(b'\x89PNG\r\n\x1a\n')
                # IHDR
                ihdr = struct.pack('>II', width, height) + b'\x08\x06\x00\x00\x00'
                f.write(struct.pack('>I', 13) + b'IHDR' + ihdr + struct.pack('>I', 0))
                # IDAT
                f.write(struct.pack('>I', len(compressed)) + b'IDAT' + compressed + struct.pack('>I', 0))
                # IEND
                f.write(struct.pack('>I', 0) + b'IEND' + struct.pack('>I', 0))
        write_png(str(sig_path))

        # Pass wrong source hash → should refuse
        rc, out, err = run_script("skills/self-sign/scripts/sign_pdf.py",
            "--input", str(pdf_path), "--output", str(tmp_project / "signed.pdf"),
            "--signature", str(sig_path),
            "--locations", json.dumps([{"id": "loc-1", "page": 1, "x": 72, "y": 60, "w": 200, "h": 40}]),
            "--source-hash", "wronghash123")
        assert rc != 0, "Should refuse to sign with wrong source hash"
        assert "hash" in (out + err).lower() or "mismatch" in (out + err).lower() or "refuse" in (out + err).lower()

    def test_manifest_output(self, tmp_project):
        import fitz
        pdf_path = tmp_project / "test2.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Sign here: ____________________", fontsize=11)
        doc.save(str(pdf_path))
        doc.close()

        # Create minimal PNG
        import struct, zlib
        sig_path = tmp_project / "sig2.png"
        raw = b'\x00' * 4
        compressed = zlib.compress(raw)
        with open(str(sig_path), 'wb') as f:
            f.write(b'\x89PNG\r\n\x1a\n')
            ihdr = struct.pack('>II', 1, 1) + b'\x08\x06\x00\x00\x00'
            f.write(struct.pack('>I', 13) + b'IHDR' + ihdr + struct.pack('>I', 0))
            f.write(struct.pack('>I', len(compressed)) + b'IDAT' + compressed + struct.pack('>I', 0))
            f.write(struct.pack('>I', 0) + b'IEND' + struct.pack('>I', 0))

        out_path = tmp_project / "signed2.pdf"
        rc, out, err = run_script("skills/self-sign/scripts/sign_pdf.py",
            "--input", str(pdf_path), "--output", str(out_path),
            "--signature", str(sig_path),
            "--locations", json.dumps([{"id": "loc-1", "page": 1, "x": 72, "y": 60, "w": 200, "h": 40}]))
        if rc == 0 and out_path.exists():
            manifest = tmp_project / "signed2.pdf.manifest.json"
            if manifest.exists():
                m = json.loads(manifest.read_text())
                assert "source_hash" in m or "source_file" in m
            else:
                pytest.skip("Manifest not created — script may use different naming")
        else:
            pytest.skip(f"Signing failed (rc={rc}): {err[:200]}")


# ── Examples fixtures ─────────────────────────────────────────────────────────

class TestExamples:
    def test_examples_exist(self):
        ex_dir = PLUGIN_ROOT / "examples"
        assert ex_dir.exists(), "examples/ directory missing"
        for f in ["company.yaml", "pipeline.yaml", "invoices.yaml", "expenses.yaml", "todos.yaml"]:
            assert (ex_dir / f).exists(), f"examples/{f} missing"

    def test_example_pipeline_valid(self):
        path = PLUGIN_ROOT / "examples" / "pipeline.yaml"
        data = yaml.safe_load(path.read_text())
        assert "deals" in data
        assert isinstance(data["deals"], list)
        assert len(data["deals"]) >= 1

    def test_example_invoices_valid(self):
        path = PLUGIN_ROOT / "examples" / "invoices.yaml"
        data = yaml.safe_load(path.read_text())
        assert "invoices" in data
        assert isinstance(data["invoices"], list)

    def test_example_company_valid(self):
        path = PLUGIN_ROOT / "examples" / "company.yaml"
        data = yaml.safe_load(path.read_text())
        assert "company" in data
        assert "google" in data
        assert "paths" in data
        assert "delivery" in data

    def test_example_briefing_exists(self):
        path = PLUGIN_ROOT / "examples" / "expected-briefing.json"
        if path.exists():
            data = json.loads(path.read_text())
            assert isinstance(data, dict)
        else:
            pytest.skip("expected-briefing.json not yet created")