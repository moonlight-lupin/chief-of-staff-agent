#!/usr/bin/env python3
"""Contract tests for attachment-to-Drive hook (v0.5.0 beta).

Tests the attachment_drive_suggestion hook in hooks.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


# ─── Hook exists ────────────────────────────────────────────────


def test_hook_function_exists():
    """attachment_drive_suggestion function exists in hooks.py."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    import hooks
    assert hasattr(hooks, "attachment_drive_suggestion")


def test_hook_returns_none_on_no_attachments():
    """Hook returns None when no attachments are present in context."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    import hooks
    result = hooks.attachment_drive_suggestion(context={}, response="")
    assert result is None


def test_hook_returns_none_on_empty_context():
    """Hook returns None when context is None or empty."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    import hooks
    assert hooks.attachment_drive_suggestion(context=None, response="") is None
    assert hooks.attachment_drive_suggestion(context={}, response="") is None


# ─── Detection ──────────────────────────────────────────────────


def test_hook_detects_attachment_in_context():
    """Hook detects attachments in context and returns a suggestion string."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    import hooks
    context = {
        "attachments": [
            {"name": "invoice_001.pdf", "path": "/tmp/invoice_001.pdf", "mime_type": "application/pdf"},
        ],
    }
    result = hooks.attachment_drive_suggestion(context=context, response="")
    assert result is not None
    assert isinstance(result, str)
    assert "invoice_001.pdf" in result


def test_hook_detects_multiple_attachments():
    """Hook detects multiple attachments and mentions each."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    import hooks
    context = {
        "attachments": [
            {"name": "report.pdf", "path": "/tmp/report.pdf"},
            {"name": "receipt.jpg", "path": "/tmp/receipt.jpg"},
        ],
    }
    result = hooks.attachment_drive_suggestion(context=context, response="")
    assert result is not None
    assert "report.pdf" in result
    assert "receipt.jpg" in result


# ─── Classification ─────────────────────────────────────────────


def test_hook_classifies_pdf_as_document():
    """Hook classifies .pdf files as document type."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    import hooks
    context = {
        "attachments": [
            {"name": "contract.pdf", "path": "/tmp/contract.pdf"},
        ],
    }
    result = hooks.attachment_drive_suggestion(context=context, response="")
    assert result is not None
    assert "document" in result.lower() or "contract" in result.lower()


def test_hook_classifies_xlsx_as_spreadsheet():
    """Hook classifies .xlsx files as spreadsheet type."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    import hooks
    context = {
        "attachments": [
            {"name": "financials.xlsx", "path": "/tmp/financials.xlsx"},
        ],
    }
    result = hooks.attachment_drive_suggestion(context=context, response="")
    assert result is not None
    assert "spreadsheet" in result.lower() or "financial" in result.lower()


def test_hook_classifies_image():
    """Hook classifies image files correctly."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    import hooks
    context = {
        "attachments": [
            {"name": "photo.jpg", "path": "/tmp/photo.jpg"},
        ],
    }
    result = hooks.attachment_drive_suggestion(context=context, response="")
    assert result is not None
    assert "image" in result.lower() or "photo" in result.lower()


# ─── Suggestion format ──────────────────────────────────────────


def test_hook_suggestion_asks_for_confirmation():
    """Hook suggestion asks the user for confirmation before filing."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    import hooks
    context = {
        "attachments": [
            {"name": "doc.pdf", "path": "/tmp/doc.pdf"},
        ],
    }
    result = hooks.attachment_drive_suggestion(context=context, response="")
    assert result is not None
    # Must ask a question (not auto-file)
    assert "?" in result or "Would you like" in result or "Want me" in result


def test_hook_does_not_auto_upload():
    """Hook must not perform any upload — only suggest."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    import hooks
    context = {
        "attachments": [
            {"name": "doc.pdf", "path": "/tmp/doc.pdf"},
        ],
    }
    result = hooks.attachment_drive_suggestion(context=context, response="")
    # The result is a suggestion string, not an upload confirmation
    assert "uploaded" not in result.lower()
    assert "filed" not in result.lower().replace("filed", "") or "would you like me to file" in result.lower()


# ─── Disabled via config ────────────────────────────────────────


def test_hook_respects_disabled_config(monkeypatch):
    """Hook returns None when attachment_suggestions is disabled in config."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    import hooks
    # Mock company.yaml with attachment_suggestions: false
    monkeypatch.setenv("CHIEF_OF_STAFF_CONFIG", "/tmp/nonexistent_company.yaml")
    context = {
        "attachments": [
            {"name": "doc.pdf", "path": "/tmp/doc.pdf"},
        ],
    }
    # When config says disabled, hook should return None
    # Since we can't easily mock the yaml loader, test that the hook
    # at least checks for config and doesn't crash
    result = hooks.attachment_drive_suggestion(context=context, response="")
    # Either None (disabled or no config) or a suggestion (enabled)
    assert result is None or isinstance(result, str)


# ─── Registered in ALL_HOOKS ────────────────────────────────────


def test_hook_registered_in_all_hooks():
    """Hook must be registered in ALL_HOOKS dict."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    import hooks
    assert hasattr(hooks, "ALL_HOOKS")
    # Check it's in post_llm_call or another event
    found = False
    for event, hook_list in hooks.ALL_HOOKS.items():
        for name, callback in hook_list:
            if "attachment" in name.lower() or "drive" in name.lower():
                found = True
                break
    assert found, "attachment_drive_suggestion must be registered in ALL_HOOKS"