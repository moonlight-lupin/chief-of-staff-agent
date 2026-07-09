#!/usr/bin/env python3
"""Tests for shared/scripts/config_loader.py"""

import os
import tempfile
from pathlib import Path

import pytest

# conftest adds SHARED_SCRIPTS to sys.path
from config_loader import Config, load_config, get_config_dir, get_project_root


class TestConfigLoading:
    def test_load_valid_config(self, sample_company_yaml):
        config = load_config(str(sample_company_yaml))
        assert config is not None
        assert config["company"]["name"] == "Test Company Pte Ltd"
        assert config.company.name == "Test Company Pte Ltd"
        assert config["company"]["jurisdiction"] == "SG"
        assert config["google"]["domain"] == "test.com"

    def test_attribute_access(self, sample_company_yaml):
        config = load_config(str(sample_company_yaml))
        assert config.company.jurisdiction == "SG"
        assert config.delivery.channel == "telegram"
        assert config.calendar.reminder_minutes == 15

    def test_dict_access_works(self, sample_company_yaml):
        config = load_config(str(sample_company_yaml))
        assert config["company"]["currency"] == "SGD"
        assert config["delivery"]["timezone"] == "Asia/Singapore"

    def test_missing_file_returns_error(self, tmp_path):
        result = load_config(str(tmp_path / "nonexistent.yaml"))
        # Should return None or raise — check behavior
        if result is not None:
            # If it returns a Config with error, the source_path should be None
            assert result.source_path is None or result.get("error") is not None

    def test_config_dir_is_under_plugin(self):
        d = get_config_dir()
        assert d.name == "config"
        assert d.parent.name == "shared"
        assert "chief-of-staff" in str(d)

    def test_get_project_root(self, sample_company_yaml):
        config = load_config(str(sample_company_yaml))
        root = get_project_root(config)
        assert root is not None
        assert "projects" in str(root)

    def test_env_var_override(self, sample_company_yaml, monkeypatch):
        monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(sample_company_yaml))
        config = load_config()  # no explicit path
        assert config is not None
        assert config.company.name == "Test Company Pte Ltd"

    def test_to_plain_dict(self, sample_company_yaml):
        config = load_config(str(sample_company_yaml))
        d = config.to_plain_dict()
        assert isinstance(d, dict)
        assert d["company"]["name"] == "Test Company Pte Ltd"
        # Ensure nested Config objects are unwrapped
        assert isinstance(d["company"], dict)
        assert not isinstance(d["company"], Config)

    def test_sales_stages_list(self, sample_company_yaml):
        config = load_config(str(sample_company_yaml))
        stages = config["sales_stages"]
        assert isinstance(stages, list)
        assert "Lead" in stages
        assert "Paid" in stages
        assert len(stages) == 6

    def test_source_path_tracked(self, sample_company_yaml):
        config = load_config(str(sample_company_yaml))
        assert config.source_path == Path(sample_company_yaml)