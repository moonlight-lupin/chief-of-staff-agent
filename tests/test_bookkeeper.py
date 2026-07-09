#!/usr/bin/env python3
"""Tests for bookkeeper — invoices, expenses, P&L report."""

import sys
import yaml
from pathlib import Path

import pytest

# Add scripts to path for pl_report
BOOKKEEPER_SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "bookkeeper" / "scripts"
if str(BOOKKEEPER_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(BOOKKEEPER_SCRIPTS))


class TestInvoiceSchema:
    def test_invoices_load(self, tmp_project_dir):
        with open(tmp_project_dir / "invoices.yaml") as f:
            data = yaml.safe_load(f)
        assert "invoices" in data
        assert len(data["invoices"]) == 3

    def test_invoice_required_fields(self, tmp_project_dir):
        with open(tmp_project_dir / "invoices.yaml") as f:
            data = yaml.safe_load(f)
        for inv in data["invoices"]:
            assert "id" in inv
            assert "direction" in inv
            assert "counterparty" in inv
            assert "amount" in inv
            assert "issue_date" in inv
            assert "due_date" in inv
            assert "status" in inv

    def test_direction_values(self, tmp_project_dir):
        with open(tmp_project_dir / "invoices.yaml") as f:
            data = yaml.safe_load(f)
        for inv in data["invoices"]:
            assert inv["direction"] in ("sent", "received"), f"Bad direction: {inv['direction']}"

    def test_invoice_ids_unique(self, tmp_project_dir):
        with open(tmp_project_dir / "invoices.yaml") as f:
            data = yaml.safe_load(f)
        ids = [i["id"] for i in data["invoices"]]
        assert len(ids) == len(set(ids))

    def test_sent_invoice_has_deal_id(self, tmp_project_dir):
        with open(tmp_project_dir / "invoices.yaml") as f:
            data = yaml.safe_load(f)
        sent = [i for i in data["invoices"] if i["direction"] == "sent"]
        for inv in sent:
            assert inv.get("deal_id") is not None, f"Sent invoice {inv['id']} missing deal_id"


class TestExpenseSchema:
    def test_expenses_load(self, tmp_project_dir):
        with open(tmp_project_dir / "expenses.yaml") as f:
            data = yaml.safe_load(f)
        assert "expenses" in data
        assert len(data["expenses"]) == 3

    def test_expense_required_fields(self, tmp_project_dir):
        with open(tmp_project_dir / "expenses.yaml") as f:
            data = yaml.safe_load(f)
        for exp in data["expenses"]:
            assert "id" in exp
            assert "category" in exp
            assert "vendor" in exp
            assert "amount" in exp
            assert "date" in exp
            assert "status" in exp

    def test_expense_ids_unique(self, tmp_project_dir):
        with open(tmp_project_dir / "expenses.yaml") as f:
            data = yaml.safe_load(f)
        ids = [e["id"] for e in data["expenses"]]
        assert len(ids) == len(set(ids))


class TestPLReport:
    def test_report_generates(self, tmp_project_dir):
        from pl_report import generate_report, parse_month
        window = parse_month("2026-07")
        report = generate_report(tmp_project_dir, window, "SGD")
        assert isinstance(report, str)
        assert len(report) > 0

    def test_report_contains_revenue(self, tmp_project_dir):
        from pl_report import generate_report, parse_month
        window = parse_month("2026-07")
        report = generate_report(tmp_project_dir, window, "SGD")
        # INV-002 was paid on 2026-06-10, not in July — so no revenue in July
        # But the report should still have a revenue section
        assert "Revenue" in report or "revenue" in report.lower()

    def test_report_contains_expenses(self, tmp_project_dir):
        from pl_report import generate_report, parse_month
        window = parse_month("2026-07")
        report = generate_report(tmp_project_dir, window, "SGD")
        # EXP-001, EXP-002, EXP-003 all in July
        assert "Expense" in report or "expense" in report.lower()

    def test_report_contains_net(self, tmp_project_dir):
        from pl_report import generate_report, parse_month
        window = parse_month("2026-07")
        report = generate_report(tmp_project_dir, window, "SGD")
        assert "Net" in report or "net" in report.lower()

    def test_report_contains_outstanding(self, tmp_project_dir):
        from pl_report import generate_report, parse_month
        window = parse_month("2026-07")
        report = generate_report(tmp_project_dir, window, "SGD")
        # Should mention outstanding AR or AP
        assert "Outstanding" in report or "outstanding" in report.lower() or "AR" in report

    def test_parse_month_valid(self):
        from pl_report import parse_month
        w = parse_month("2026-07")
        assert w.month == "2026-07"
        assert w.start.month == 7
        assert w.end.month == 7
        assert w.end.day == 31  # July has 31 days

    def test_parse_month_invalid(self):
        from pl_report import parse_month
        with pytest.raises(Exception):
            parse_month("2026-13")

    def test_parse_month_february_leap(self):
        from pl_report import parse_month
        w = parse_month("2024-02")
        assert w.end.day == 29  # 2024 is a leap year