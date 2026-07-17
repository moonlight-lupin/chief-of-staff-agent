#!/usr/bin/env python3
"""Provider-neutral workspace client for mail, calendar, files.

Method names are provider-neutral (mail_*, files_*, calendar_*) so a Microsoft
365 provider or a Claude-native agent provider can implement the same surface.
The Gmail/Drive-flavored names (gmail_*, drive_*) remain as DEPRECATED thin
aliases on the base class that emit a DeprecationWarning and delegate to the
neutral method. Providers implement ONLY the neutral names.

Usage:
    from workspace_client import get_workspace_client
    client = get_workspace_client(config)
    emails = client.mail_search("is:unread", max_results=10)
    events = client.calendar_list("2026-07-09", "2026-07-10")
    files = client.files_search("name = 'NDA'", max_results=10)
"""
from __future__ import annotations

import abc
import importlib
import sys
import warnings
from pathlib import Path
from typing import Any, Mapping

# Ensure shared/scripts is importable
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))


class WorkspaceClient(abc.ABC):
    """Abstract base for workspace providers (mail, calendar, files).

    Providers implement the neutral methods below. The gmail_*/drive_* aliases
    at the bottom of the class are deprecated shims — do not override them.
    """

    # ── Mail (neutral) ─────────────────────────────────────────────────

    @abc.abstractmethod
    def mail_search(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        """Search mail messages. Returns list of message dicts."""
        ...

    def mail_create_draft(self, to: str, subject: str, body: str,
                          cc: str | None = None) -> dict[str, Any]:
        """Create a mail draft. Returns draft metadata."""
        raise NotImplementedError(f"{self.__class__.__name__} does not support mail_create_draft")

    def mail_send(self, to: str, subject: str, body: str,
                  cc: str | None = None) -> dict[str, Any]:
        """Send an email. Destructive — requires explicit user approval.

        Prefer the pending-action queue (``send_email.py prepare → approve →
        execute``). Direct calls need ``CHIEF_OF_STAFF_ALLOW_DESTRUCTIVE=1``
        (and interactive confirm, or ``CHIEF_OF_STAFF_AUTO_APPROVE=1`` when
        approval already happened out-of-band).
        """
        raise NotImplementedError(f"{self.__class__.__name__} does not support mail_send")

    def mail_list_folders(self, include_hidden: bool = False,
                          max_results: int = 100) -> list[dict[str, Any]]:
        """List mail folders (Outlook mailFolders). Read-only.

        Each item is ``{id, name, ...}``. Custom-folder moves need the ``id``
        from this list (display names are not valid destination ids).
        """
        raise NotImplementedError(f"{self.__class__.__name__} does not support mail_list_folders")

    def mail_move_to_folder(self, message_id: str, folder_id: str) -> dict[str, Any]:
        """Move a message to a folder id or well-known name. Reversible."""
        raise NotImplementedError(f"{self.__class__.__name__} does not support mail_move_to_folder")

    def mail_archive(self, message_id: str) -> dict[str, Any]:
        """Archive a mail message (remove from inbox). Reversible."""
        raise NotImplementedError(f"{self.__class__.__name__} does not support mail_archive")

    def mail_unarchive(self, message_id: str) -> dict[str, Any]:
        """Restore an archived mail message (return to inbox)."""
        raise NotImplementedError(f"{self.__class__.__name__} does not support mail_unarchive")

    def mail_trash(self, message_id: str) -> dict[str, Any]:
        """Move a mail message to trash. Reversible."""
        raise NotImplementedError(f"{self.__class__.__name__} does not support mail_trash")

    def mail_untrash(self, message_id: str) -> dict[str, Any]:
        """Restore a trashed mail message."""
        raise NotImplementedError(f"{self.__class__.__name__} does not support mail_untrash")

    def mail_list_tags(self) -> list[dict[str, Any]]:
        """List all mail tags (Gmail labels ≈ Outlook categories). Read-only."""
        raise NotImplementedError(f"{self.__class__.__name__} does not support mail_list_tags")

    def mail_tag(self, message_id: str, tag_id: str) -> dict[str, Any]:
        """Apply an existing tag (Gmail label / Outlook category) to a message."""
        raise NotImplementedError(f"{self.__class__.__name__} does not support mail_tag")

    def mail_create_tag(self, name: str) -> dict[str, Any]:
        """Create a new tag (Gmail label / Outlook category)."""
        raise NotImplementedError(f"{self.__class__.__name__} does not support mail_create_tag")

    # ── Calendar (already neutral) ─────────────────────────────────────

    @abc.abstractmethod
    def calendar_list(self, start: str, end: str) -> list[dict[str, Any]]:
        """List calendar events between start and end dates (ISO format)."""
        ...

    def calendar_create(self, title: str, start: str, end: str,
                        attendees: list[str] | None = None,
                        description: str | None = None) -> dict[str, Any]:
        """Create a calendar event. Returns event metadata."""
        raise NotImplementedError(f"{self.__class__.__name__} does not support calendar_create")

    def calendar_update(self, event_id: str, **fields: Any) -> dict[str, Any]:
        """Update a calendar event. Returns updated event metadata."""
        raise NotImplementedError(f"{self.__class__.__name__} does not support calendar_update")

    def calendar_cancel(self, event_id: str) -> dict[str, Any]:
        """Cancel a calendar event (set status to cancelled). Reversible via update."""
        raise NotImplementedError(f"{self.__class__.__name__} does not support calendar_cancel")

    # ── Files (neutral) ────────────────────────────────────────────────

    @abc.abstractmethod
    def files_search(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        """Search files. Returns list of file dicts."""
        ...

    @abc.abstractmethod
    def files_upload(self, file_path: str, parent_id: str | None = None) -> dict[str, Any]:
        """Upload a file. Returns uploaded file metadata."""
        ...

    def files_download(self, file_id: str, output_path: str) -> dict[str, Any]:
        """Download a file. Returns download metadata."""
        raise NotImplementedError(f"{self.__class__.__name__} does not support files_download")

    def files_trash(self, file_id: str) -> dict[str, Any]:
        """Move a file to trash. Reversible."""
        raise NotImplementedError(f"{self.__class__.__name__} does not support files_trash")

    def files_untrash(self, file_id: str) -> dict[str, Any]:
        """Restore a trashed file from Drive trash / OneDrive recycle bin."""
        raise NotImplementedError(f"{self.__class__.__name__} does not support files_untrash")

    @abc.abstractmethod
    def health_check(self) -> bool:
        """Return True if the provider is healthy and authenticated."""
        ...

    # ── Capability reporting ───────────────────────────────────────────

    @property
    def provider_name(self) -> str:
        """Return the provider identifier (e.g. 'google_api', 'composio')."""
        return getattr(self, "_provider_name", "unknown")

    def capabilities(self) -> dict[str, bool]:
        """Return capability dict for this provider."""
        from workspace_capabilities import get_capabilities
        return get_capabilities(self.provider_name)

    def supports(self, action: str) -> bool:
        """Check if this provider supports a specific action (neutral or legacy key)."""
        return self.capabilities().get(action, False)

    # ── Deprecated Gmail/Drive-flavored aliases ────────────────────────
    # Defined ONCE here as thin wrappers so all ~50 legacy call sites keep
    # working. Each emits a DeprecationWarning and delegates to the neutral
    # method. Do NOT override these in providers.

    def gmail_search(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        warnings.warn("gmail_search is deprecated; use mail_search", DeprecationWarning, stacklevel=2)
        return self.mail_search(query, max_results)

    def gmail_create_draft(self, to: str, subject: str, body: str,
                           cc: str | None = None) -> dict[str, Any]:
        warnings.warn("gmail_create_draft is deprecated; use mail_create_draft", DeprecationWarning, stacklevel=2)
        return self.mail_create_draft(to, subject, body, cc)

    def gmail_send(self, to: str, subject: str, body: str,
                   cc: str | None = None) -> dict[str, Any]:
        warnings.warn("gmail_send is deprecated; use mail_send", DeprecationWarning, stacklevel=2)
        return self.mail_send(to, subject, body, cc)

    def gmail_archive(self, message_id: str) -> dict[str, Any]:
        warnings.warn("gmail_archive is deprecated; use mail_archive", DeprecationWarning, stacklevel=2)
        return self.mail_archive(message_id)

    def gmail_unarchive(self, message_id: str) -> dict[str, Any]:
        warnings.warn("gmail_unarchive is deprecated; use mail_unarchive", DeprecationWarning, stacklevel=2)
        return self.mail_unarchive(message_id)

    def gmail_trash(self, message_id: str) -> dict[str, Any]:
        warnings.warn("gmail_trash is deprecated; use mail_trash", DeprecationWarning, stacklevel=2)
        return self.mail_trash(message_id)

    def gmail_untrash(self, message_id: str) -> dict[str, Any]:
        warnings.warn("gmail_untrash is deprecated; use mail_untrash", DeprecationWarning, stacklevel=2)
        return self.mail_untrash(message_id)

    def gmail_list_labels(self) -> list[dict[str, Any]]:
        warnings.warn("gmail_list_labels is deprecated; use mail_list_tags", DeprecationWarning, stacklevel=2)
        return self.mail_list_tags()

    def gmail_label(self, message_id: str, label_id: str) -> dict[str, Any]:
        warnings.warn("gmail_label is deprecated; use mail_tag", DeprecationWarning, stacklevel=2)
        return self.mail_tag(message_id, label_id)

    def gmail_create_label(self, label_name: str) -> dict[str, Any]:
        warnings.warn("gmail_create_label is deprecated; use mail_create_tag", DeprecationWarning, stacklevel=2)
        return self.mail_create_tag(label_name)

    def drive_search(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        warnings.warn("drive_search is deprecated; use files_search", DeprecationWarning, stacklevel=2)
        return self.files_search(query, max_results)

    def drive_upload(self, file_path: str, parent_id: str | None = None) -> dict[str, Any]:
        warnings.warn("drive_upload is deprecated; use files_upload", DeprecationWarning, stacklevel=2)
        return self.files_upload(file_path, parent_id)

    def drive_download(self, file_id: str, output_path: str) -> dict[str, Any]:
        warnings.warn("drive_download is deprecated; use files_download", DeprecationWarning, stacklevel=2)
        return self.files_download(file_id, output_path)

    def drive_trash(self, file_id: str) -> dict[str, Any]:
        warnings.warn("drive_trash is deprecated; use files_trash", DeprecationWarning, stacklevel=2)
        return self.files_trash(file_id)

    def drive_untrash(self, file_id: str) -> dict[str, Any]:
        warnings.warn("drive_untrash is deprecated; use files_untrash", DeprecationWarning, stacklevel=2)
        return self.files_untrash(file_id)


# ── Provider registry ──────────────────────────────────────────────────
# name -> (module path, factory attribute). Imports are lazy (resolved at
# lookup time) so providers whose modules don't exist yet ("m365", "agent")
# can be pre-registered without breaking imports. The factory attribute may be
# a class or a factory function; both are called with (config).
_PROVIDER_REGISTRY: dict[str, tuple[str, str]] = {
    "google_api": ("providers.google_workspace", "GoogleWorkspaceClient"),
    "composio": ("providers.composio_mcp_workspace", "get_composio_client"),
    # Pre-registered; modules authored by later phases.
    "m365": ("providers.m365_graph", "M365GraphClient"),
    "agent": ("providers.agent_workspace", "AgentWorkspaceClient"),
}


def register_provider(name: str, module: str, attr: str) -> None:
    """Register (or override) a workspace provider.

    Args:
        name: provider key used in config (integrations.workspace.provider).
        module: importable module path (e.g. "providers.m365_graph").
        attr: attribute on that module — a class or factory callable taking config.
    """
    _PROVIDER_REGISTRY[str(name)] = (str(module), str(attr))


def registered_providers() -> list[str]:
    """Return the sorted list of registered provider names."""
    return sorted(_PROVIDER_REGISTRY)


def get_workspace_client(config: Any) -> WorkspaceClient:
    """Factory: return a WorkspaceClient based on config.

    Reads config["integrations"]["workspace"]["provider"].
    Falls back to "google_api" if the integrations section is missing.

    Raises:
        ValueError: the provider name is not registered.
        ImportError: the provider is registered but its module cannot be imported
            (e.g. a pre-registered provider whose module ships in a later phase).
    """
    integrations = config.get("integrations", {}) if isinstance(config, Mapping) else {}
    workspace_cfg = integrations.get("workspace", {}) if isinstance(integrations, Mapping) else {}
    provider = str(workspace_cfg.get("provider", "google_api") or "google_api")

    if provider not in _PROVIDER_REGISTRY:
        raise ValueError(
            f"Unknown workspace provider: {provider}. "
            f"Registered providers: {', '.join(registered_providers())}."
        )

    module_name, attr = _PROVIDER_REGISTRY[provider]
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ImportError(
            f"Workspace provider '{provider}' is registered to module "
            f"'{module_name}', which could not be imported: {exc}. "
            f"This provider may be authored in a later phase."
        ) from exc

    factory = getattr(module, attr)
    return factory(config)


def _main() -> int:
    import argparse, json
    parser = argparse.ArgumentParser(description="Workspace client factory")
    parser.add_argument("--config", help="Path to company.yaml")
    parser.add_argument("--status", action="store_true", help="Print provider status")
    args = parser.parse_args()

    if args.config:
        try:
            import yaml
            with open(args.config) as f:
                config = yaml.safe_load(f) or {}
        except Exception as exc:
            print(f"Error loading config: {exc}", file=sys.stderr)
            return 1
    else:
        config = {}

    if args.status:
        try:
            client = get_workspace_client(config)
            healthy = client.health_check()
            print(json.dumps({"provider": client.__class__.__name__, "healthy": healthy}))
        except NotImplementedError as exc:
            print(json.dumps({"provider": "composio", "healthy": False, "error": str(exc)}))
        except Exception as exc:
            print(json.dumps({"provider": "unknown", "healthy": False, "error": str(exc)}))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
