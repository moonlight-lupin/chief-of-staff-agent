#!/usr/bin/env python3
"""Shared fixtures for chief-of-staff plugin tests."""

import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PLUGIN_ROOT / "shared" / "scripts"

# Ensure shared scripts are importable
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SHARED_SCRIPTS))


@pytest.fixture
def tmp_project_dir():
    """Create a temporary project directory with sample YAML data files."""
    with tempfile.TemporaryDirectory(dir="/root") as tmpdir:
        root = Path(tmpdir)
        today = date.today()
        stale_activity = (today - timedelta(days=30)).isoformat()
        recent_activity = (today - timedelta(days=5)).isoformat()

        # Sample pipeline.yaml
        (root / "pipeline.yaml").write_text(f"""\
deals:
  - id: deal-001
    client_name: Acme Corp
    contact_name: John Tan
    contact_email: john@acme.com
    stage: Proposal Sent
    value: 4500
    currency: SGD
    created: "2026-06-15"
    last_activity: "{stale_activity}"
    documents:
      - type: NDA
        path: 02_Clients/Acme Corp/NDA/NDA_signed.pdf
        status: signed
    notes: Follow up after board meeting
  - id: deal-002
    client_name: Beta Ltd
    contact_name: Jane Lee
    contact_email: jane@beta.com
    stage: Lead
    value: 2000
    currency: SGD
    created: "2026-07-05"
    last_activity: "{recent_activity}"
    documents: []
    notes: ""
""")

        # Sample invoices.yaml
        (root / "invoices.yaml").write_text("""\
invoices:
  - id: INV-001
    direction: sent
    counterparty: Acme Corp
    deal_id: deal-001
    amount: 4500
    currency: SGD
    issue_date: "2026-07-01"
    due_date: "2026-07-15"
    status: sent
    paid_date: null
    document_path: 04_Finance/Invoices_Sent/INV-001.pdf
    notes: ""
  - id: INV-002
    direction: sent
    counterparty: Beta Ltd
    deal_id: deal-002
    amount: 2000
    currency: SGD
    issue_date: "2026-06-01"
    due_date: "2026-06-15"
    status: paid
    paid_date: "2026-06-10"
    document_path: 04_Finance/Invoices_Sent/INV-002.pdf
    notes: ""
  - id: BILL-001
    direction: received
    counterparty: Google
    deal_id: null
    amount: 12
    currency: SGD
    issue_date: "2026-07-01"
    due_date: "2026-07-31"
    status: received
    paid_date: null
    document_path: 04_Finance/Invoices_Received/BILL-001.pdf
    notes: Monthly Google Workspace
""")

        # Sample expenses.yaml
        (root / "expenses.yaml").write_text("""\
expenses:
  - id: EXP-001
    category: software
    vendor: Google
    amount: 12
    currency: SGD
    date: "2026-07-01"
    status: paid
    document_path: 04_Finance/Receipts/EXP-001.pdf
    recurring: monthly
    notes: ""
  - id: EXP-002
    category: rent
    vendor: Office Landlord
    amount: 800
    currency: SGD
    date: "2026-07-01"
    status: paid
    document_path: 04_Finance/Receipts/EXP-002.pdf
    recurring: monthly
    notes: ""
  - id: EXP-003
    category: travel
    vendor: Singapore Airlines
    amount: 350
    currency: SGD
    date: "2026-07-05"
    status: paid
    document_path: 04_Finance/Receipts/EXP-003.pdf
    recurring: null
    notes: Client visit to KL
""")

        # Sample todos.yaml
        (root / "todos.yaml").write_text(
            "todos:\n"
            "  - id: todo-001\n"
            "    title: Follow up with John re proposal\n"
            "    priority: high\n"
            "    due: '2026-07-12'\n"
            "    status: open\n"
            "    source: briefing\n"
            "    tags: [sales, acme-corp]\n"
            "    created: '2026-07-09'\n"
            "    completed: null\n"
            "  - id: todo-002\n"
            "    title: Review contract from Beta\n"
            "    priority: medium\n"
            "    due: '2026-07-20'\n"
            "    status: open\n"
            "    source: manual\n"
            "    tags: [legal, beta-ltd]\n"
            "    created: '2026-07-08'\n"
            "    completed: null\n"
            "  - id: todo-003\n"
            "    title: Send invoice to Acme\n"
            "    priority: high\n"
            "    due: '2026-07-01'\n"
            "    status: done\n"
            "    source: meeting\n"
            "    tags: [finance, acme-corp]\n"
            "    created: '2026-06-28'\n"
            "    completed: '2026-07-01'\n"
        )

        yield root


@pytest.fixture
def sample_company_yaml():
    """Create a minimal company.yaml for testing."""
    import tempfile
    with tempfile.TemporaryDirectory(dir="/root") as tmpdir:
        config = Path(tmpdir) / "company.yaml"
        config.write_text("""\
company:
  name: "Test Company Pte Ltd"
  jurisdiction: SG
  incorporation_date: "2024-01-15"
  financial_year_end: "31 Dec"
  currency: SGD
  business_type: professional_services

google:
  service_account_path: "~/.hermes/test_service_account.json"
  domain: "test.com"
  delegate_email: "founder@test.com"

paths:
  project_root: "~/.hermes/projects/test/"
  wiki_path: "~/.hermes/projects/test/wiki/"
  templates: "~/.hermes/plugins/chief-of-staff/shared/templates/"

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
  signature_image: "shared/assets/signature.png"
  auto_date: true
  output_format: pdf
  party_aliases:
    - "Service Provider"
    - "Consultant"

backup:
  enabled: true
  schedule: "0 3 * * 0"
  retention_weekly: 4
  retention_monthly: 12
  drive_folder: "09_Backups/"
  exclude:
    - ".env"
    - "auth.json"
    - "state.db"
    - "sessions/"
    - "logs/"
""")
        yield str(config)