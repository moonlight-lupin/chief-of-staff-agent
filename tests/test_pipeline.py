#!/usr/bin/env python3
"""Tests for pipeline.yaml schema and operations."""

import yaml
from pathlib import Path
from datetime import datetime, timedelta


class TestPipelineYAMLSchema:
    def test_pipeline_loads(self, tmp_project_dir):
        with open(tmp_project_dir / "pipeline.yaml") as f:
            data = yaml.safe_load(f)
        assert "deals" in data
        assert isinstance(data["deals"], list)
        assert len(data["deals"]) == 2

    def test_deal_required_fields(self, tmp_project_dir):
        with open(tmp_project_dir / "pipeline.yaml") as f:
            data = yaml.safe_load(f)
        for deal in data["deals"]:
            assert "id" in deal
            assert "client_name" in deal
            assert "stage" in deal
            assert "created" in deal
            assert "last_activity" in deal

    def test_deal_ids_unique(self, tmp_project_dir):
        with open(tmp_project_dir / "pipeline.yaml") as f:
            data = yaml.safe_load(f)
        ids = [d["id"] for d in data["deals"]]
        assert len(ids) == len(set(ids)), "Duplicate deal IDs"

    def test_deal_stages_valid(self, tmp_project_dir):
        with open(tmp_project_dir / "pipeline.yaml") as f:
            data = yaml.safe_load(f)
        valid_stages = {"Lead", "Proposal Sent", "NDA Signed", "Contract Signed", "Invoiced", "Paid"}
        for deal in data["deals"]:
            assert deal["stage"] in valid_stages, f"Invalid stage: {deal['stage']}"

    def test_deal_documents_is_list(self, tmp_project_dir):
        with open(tmp_project_dir / "pipeline.yaml") as f:
            data = yaml.safe_load(f)
        for deal in data["deals"]:
            assert "documents" in deal
            assert isinstance(deal["documents"], list)

    def test_deal_value_is_numeric(self, tmp_project_dir):
        with open(tmp_project_dir / "pipeline.yaml") as f:
            data = yaml.safe_load(f)
        for deal in data["deals"]:
            if "value" in deal and deal["value"] is not None:
                assert isinstance(deal["value"], (int, float)), f"Value not numeric: {deal['value']}"

    def test_deal_001_is_acme(self, tmp_project_dir):
        with open(tmp_project_dir / "pipeline.yaml") as f:
            data = yaml.safe_load(f)
        deal = data["deals"][0]
        assert deal["client_name"] == "Acme Corp"
        assert deal["contact_email"] == "john@acme.com"
        assert deal["stage"] == "Proposal Sent"
        assert deal["value"] == 4500


class TestStaleDetection:
    def test_old_deal_is_stale(self, tmp_project_dir):
        """Deal with last_activity > 14 days ago is stale."""
        with open(tmp_project_dir / "pipeline.yaml") as f:
            data = yaml.safe_load(f)
        threshold = 14
        today = datetime.now().date()
        for deal in data["deals"]:
            last = datetime.strptime(deal["last_activity"], "%Y-%m-%d").date()
            age = (today - last).days
            if age > threshold:
                # deal-001 has last_activity 2026-07-01 which should be stale by now
                assert deal["id"] == "deal-001"

    def test_recent_deal_not_stale(self, tmp_project_dir):
        with open(tmp_project_dir / "pipeline.yaml") as f:
            data = yaml.safe_load(f)
        today = datetime.now().date()
        threshold = 14
        for deal in data["deals"]:
            last = datetime.strptime(deal["last_activity"], "%Y-%m-%d").date()
            age = (today - last).days
            if age <= threshold:
                # This deal is NOT stale
                assert deal["id"] in ("deal-001", "deal-002")