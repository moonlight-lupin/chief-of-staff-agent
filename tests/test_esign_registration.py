#!/usr/bin/env python3
"""Tests for config-gated e-sign skill registration."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _load_plugin_init():
    spec = importlib.util.spec_from_file_location("chief_of_staff_plugin_init", PLUGIN_ROOT / "__init__.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_config(path: Path, esign: dict | None = None) -> None:
    data = {
        "company": {"name": "Test Company", "jurisdiction": "SG"},
        "paths": {"project_root": str(path.parent / "project")},
    }
    if esign is not None:
        data["esign"] = esign
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def test_esign_connector_not_registered_without_url(monkeypatch, tmp_path):
    config = tmp_path / "company.yaml"
    _write_config(config, esign={"provider": "docuseal"})
    monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config))
    monkeypatch.delenv("CHIEF_OF_STAFF_SKILL_PROFILE", raising=False)

    plugin = _load_plugin_init()
    skills = plugin._get_registered_skills()

    assert "self-sign" in skills
    assert "esign-connector" not in skills


def test_esign_connector_registered_with_url(monkeypatch, tmp_path):
    config = tmp_path / "company.yaml"
    _write_config(config, esign={"provider": "docuseal", "url": "https://docuseal.example.com"})
    monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", str(config))
    monkeypatch.delenv("CHIEF_OF_STAFF_SKILL_PROFILE", raising=False)

    plugin = _load_plugin_init()
    skills = plugin._get_registered_skills()

    assert "self-sign" in skills
    assert "esign-connector" in skills
