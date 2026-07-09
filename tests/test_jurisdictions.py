#!/usr/bin/env python3
"""Tests for jurisdiction packs — validate structure and required fields."""

from pathlib import Path

import pytest
import yaml

JURISDICTIONS_DIR = Path(__file__).resolve().parents[1] / "shared" / "config" / "jurisdictions"


@pytest.fixture
def sg_pack():
    with open(JURISDICTIONS_DIR / "sg.yaml") as f:
        return yaml.safe_load(f)

@pytest.fixture
def hk_pack():
    with open(JURISDICTIONS_DIR / "hk.yaml") as f:
        return yaml.safe_load(f)

@pytest.fixture
def us_pack():
    with open(JURISDICTIONS_DIR / "us.yaml") as f:
        return yaml.safe_load(f)

@pytest.fixture
def uk_pack():
    with open(JURISDICTIONS_DIR / "uk.yaml") as f:
        return yaml.safe_load(f)


class TestJurisdictionFiles:
    def test_all_four_files_exist(self):
        for code in ["sg", "hk", "us", "uk"]:
            assert (JURISDICTIONS_DIR / f"{code}.yaml").exists(), f"Missing {code}.yaml"


class TestSGPack:
    def test_jurisdiction_code(self, sg_pack):
        assert sg_pack["jurisdiction"] == "SG"

    def test_has_statutory_list(self, sg_pack):
        assert "statutory" in sg_pack
        assert isinstance(sg_pack["statutory"], list)
        assert len(sg_pack["statutory"]) >= 4

    def test_each_entry_has_required_fields(self, sg_pack):
        for entry in sg_pack["statutory"]:
            assert "name" in entry, f"Missing name in {entry}"
            assert "frequency" in entry, f"Missing frequency in {entry}"
            assert "trigger" in entry, f"Missing trigger in {entry}"
            assert "authority" in entry, f"Missing authority in {entry}"

    def test_has_annual_return(self, sg_pack):
        names = [e["name"] for e in sg_pack["statutory"]]
        assert any("Annual Return" in n for n in names)

    def test_has_agm(self, sg_pack):
        names = [e["name"] for e in sg_pack["statutory"]]
        assert any("AGM" in n for n in names)

    def test_has_eci(self, sg_pack):
        names = [e["name"] for e in sg_pack["statutory"]]
        assert any("ECI" in n for n in names)


class TestHKPack:
    def test_jurisdiction_code(self, hk_pack):
        assert hk_pack["jurisdiction"] == "HK"

    def test_has_nar1(self, hk_pack):
        names = [e["name"] for e in hk_pack["statutory"]]
        assert any("NAR1" in n or "Annual Return" in n for n in names)

    def test_has_profits_tax(self, hk_pack):
        names = [e["name"] for e in hk_pack["statutory"]]
        assert any("Profits Tax" in n for n in names)


class TestUSPack:
    def test_jurisdiction_code(self, us_pack):
        assert us_pack["jurisdiction"] == "US"

    def test_has_federal_income_tax(self, us_pack):
        names = [e["name"] for e in us_pack["statutory"]]
        assert any("Federal Income Tax" in n for n in names)

    def test_has_estimated_tax(self, us_pack):
        names = [e["name"] for e in us_pack["statutory"]]
        assert any("Estimated Tax" in n for n in names)


class TestUKPack:
    def test_jurisdiction_code(self, uk_pack):
        assert uk_pack["jurisdiction"] == "UK"

    def test_has_confirmation_statement(self, uk_pack):
        names = [e["name"] for e in uk_pack["statutory"]]
        assert any("Confirmation Statement" in n for n in names)

    def test_has_corporation_tax(self, uk_pack):
        names = [e["name"] for e in uk_pack["statutory"]]
        assert any("Corporation Tax" in n for n in names)