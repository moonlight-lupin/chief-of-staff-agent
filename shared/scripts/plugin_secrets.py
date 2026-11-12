"""The plugin's secrets, and the one place that reads them.

Under Hermes (catalog rule 11) credentials come only through Hermes:

* every secret is declared in ``plugin.yaml`` -> ``config_schema`` as
  ``type: secret`` with its ``env`` name, so Hermes stores it through its own
  credential route and the Desktop shows a masked field;
* inside the Hermes process the value is read with
  ``agent.secret_scope.get_secret`` (profile-aware);
* the plugin's own commands (``.venv/bin/python shared/scripts/...``) run as
  child processes, so ``register()`` hands them the values per session through
  ``ctx.register_session_env(SECRETS, session_env)``. The resolver reads each
  value with ``get_secret``; an unset secret is removed from the command env.

Outside Hermes (standalone CLI, Claude Code) the command environment, plus the
optional plugin-root ``.env`` that ``config_loader`` loads only when not under
Hermes, is the source.
"""

from __future__ import annotations

import os
import sys
from typing import Mapping

# env name -> description shown in the Hermes settings form.
SECRETS: Mapping[str, str] = {
    "COMPOSIO_MCP_KEY": "Composio MCP key, for the composio workspace provider (Google or Microsoft 365 via managed OAuth).",
    "M365_CLIENT_SECRET": "Microsoft Entra app client secret, for the m365 workspace provider (client-credentials flow).",
    "GOOGLE_SERVICE_ACCOUNT_JSON": "Google service-account key JSON (the file's contents), for the google_api provider's REST writes.",
    "DOCUSEAL_API_KEY": "DocuSeal API key, for the esign-connector skill (self-hosted DocuSeal).",
    "DOCUSEAL_MCP_TOKEN": "DocuSeal MCP token, if DocuSeal is reached through its MCP server.",
    "CHIEF_OF_STAFF_WEBHOOK_SECRET": "Shared secret that signs inbound workspace webhooks (optional webhook mode).",
    "CHIEF_OF_STAFF_WEBHOOK_CHANNEL_TOKEN": "Google push-channel token checked on inbound webhooks (optional webhook mode).",
}

SA_JSON_ENV = "GOOGLE_SERVICE_ACCOUNT_JSON"
# Stands in for a key-file path when the service-account key comes from the secret.
SA_SECRET_SOURCE = f"secret:{SA_JSON_ENV}"


def _secret_scope():
    module = sys.modules.get("agent.secret_scope")
    if module is not None:
        return module
    return None


def under_hermes() -> bool:
    """True inside the Hermes process or a command Hermes started for a session."""
    if _secret_scope() is not None:
        return True
    return any(key.startswith("HERMES_SESSION_") for key in os.environ)


def get(name: str, default: str | None = None) -> str | None:
    """Read a credential: Hermes's secret scope in-process, else the command env."""
    scope = _secret_scope()
    if scope is not None:
        value = scope.get_secret(name, default)
    else:
        value = os.getenv(name, default)
    if value is None:
        return default
    value = str(value).strip()
    return value or default


def session_env(session_id: str) -> dict[str, str | None]:
    """``register_session_env`` resolver: every declared secret, via get_secret."""
    from agent.secret_scope import get_secret  # only ever called by Hermes

    out: dict[str, str | None] = {}
    for name in SECRETS:
        value = get_secret(name, None)
        out[name] = str(value) if value not in (None, "") else None
    return out
