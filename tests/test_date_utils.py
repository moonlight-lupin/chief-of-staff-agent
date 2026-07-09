#!/usr/bin/env python3
"""Tests for shared/scripts/date_utils.py"""

from datetime import date, timedelta

import pytest

from date_utils import (
    parse_date,
    days_until,
    categorize_deadline,
    is_business_day,
)


class TestParseDate:
    def test_iso_format(self):
        assert parse_date("2026-07-09") == date(2026, 7, 9)

    def test_dd_mm_yyyy(self):
        assert parse_date("09/07/2026") == date(2026, 7, 9)

    def test_dd_mon_yyyy(self):
        assert parse_date("09 Jul 2026") == date(2026, 7, 9)

    def test_dd_month_yyyy(self):
        assert parse_date("09 July 2026") == date(2026, 7, 9)

    def test_date_object(self):
        d = date(2026, 7, 9)
        assert parse_date(d) == d

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            parse_date("not a date")


class TestDaysUntil:
    def test_today_is_zero(self):
        assert days_until(date.today()) == 0

    def test_tomorrow_is_one(self):
        assert days_until(date.today() + timedelta(days=1)) == 1

    def test_yesterday_is_negative(self):
        assert days_until(date.today() - timedelta(days=1)) == -1

    def test_string_input(self):
        future = (date.today() + timedelta(days=30)).strftime("%Y-%m-%d")
        assert days_until(future) == 30


class TestCategorizeDeadline:
    def test_overdue(self):
        past = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
        assert categorize_deadline(past) == "overdue"

    def test_today_is_within_7(self):
        today = date.today().strftime("%Y-%m-%d")
        assert categorize_deadline(today) == "within_7"

    def test_within_7(self):
        d = (date.today() + timedelta(days=5)).strftime("%Y-%m-%d")
        assert categorize_deadline(d) == "within_7"

    def test_within_30(self):
        d = (date.today() + timedelta(days=20)).strftime("%Y-%m-%d")
        assert categorize_deadline(d) == "within_30"

    def test_future(self):
        d = (date.today() + timedelta(days=60)).strftime("%Y-%m-%d")
        assert categorize_deadline(d) == "future"

    def test_exactly_7_days(self):
        d = (date.today() + timedelta(days=7)).strftime("%Y-%m-%d")
        assert categorize_deadline(d) == "within_7"

    def test_exactly_30_days(self):
        d = (date.today() + timedelta(days=30)).strftime("%Y-%m-%d")
        assert categorize_deadline(d) == "within_30"

    def test_exactly_31_days(self):
        d = (date.today() + timedelta(days=31)).strftime("%Y-%m-%d")
        assert categorize_deadline(d) == "future"


class TestIsBusinessDay:
    def test_weekday_is_business_day(self):
        # Find the next Monday
        today = date.today()
        days_ahead = (0 - today.weekday()) % 7
        monday = today + timedelta(days=days_ahead)
        assert is_business_day(monday.strftime("%Y-%m-%d"), "SG") is True

    def test_saturday_is_not_business_day(self):
        today = date.today()
        days_ahead = (5 - today.weekday()) % 7
        saturday = today + timedelta(days=days_ahead)
        if days_ahead == 0:  # if today is Saturday, go to next Saturday
            saturday += timedelta(days=7)
        assert is_business_day(saturday.strftime("%Y-%m-%d"), "SG") is False

    def test_sunday_is_not_business_day(self):
        today = date.today()
        days_ahead = (6 - today.weekday()) % 7
        sunday = today + timedelta(days=days_ahead)
        if days_ahead == 0:
            sunday += timedelta(days=7)
        assert is_business_day(sunday.strftime("%Y-%m-%d"), "SG") is False

    def test_sg_public_holiday(self):
        # 2026-01-01 is New Year's Day in SG
        assert is_business_day("2026-01-01", "SG") is False

    def test_hk_public_holiday(self):
        # 2026-01-01 is a holiday in HK
        assert is_business_day("2026-01-01", "HK") is False

    def test_us_public_holiday(self):
        # 2026-01-01 is a US federal holiday
        assert is_business_day("2026-01-01", "US") is False

    def test_uk_public_holiday(self):
        # 2026-01-01 is a UK bank holiday
        assert is_business_day("2026-01-01", "UK") is False

    def test_unknown_jurisdiction_still_checks_weekends(self):
        today = date.today()
        days_ahead = (5 - today.weekday()) % 7
        saturday = today + timedelta(days=days_ahead)
        if days_ahead == 0:
            saturday += timedelta(days=7)
        assert is_business_day(saturday.strftime("%Y-%m-%d"), "XX") is False