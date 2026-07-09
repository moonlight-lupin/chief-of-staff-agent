#!/usr/bin/env python3
"""Onboarding script for workspace provider setup.

Usage:
    python connect_workspace.py --status
    python connect_workspace.py --provider google_api
    python connect_workspace.py --provider composio --print-next-steps
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))


def _load_config(config_path: str | None) -> dict[str, Any]:
    if config_path:
        try:
            import yaml
            with open(config_path) as f:
                return yaml.safe_load(f) or {}
        except Exception as exc:
            print(f"Error loading config: {exc}", file=sys.stderr)
            return {}
    # Try CHIEF_OF_STAFF_CONFIG env var
    env_path = os.getenv("CHIEF_OF_STAFF_CONFIG")
    if env_path and Path(env_path).exists():
        try:
            import yaml
            with open(env_path) as f:
                return yaml.safe_load(f) or {}
        except Exception:
            pass
    return {}


def cmd_status(config: dict[str, Any]) -> int:
    """Print current workspace provider status."""
    integrations = config.get("integrations", {}) if isinstance(config, Mapping) else {}
    workspace = integrations.get("workspace", {}) if isinstance(integrations, Mapping) else {}
    provider = workspace.get("provider", "google_api")
    mode = workspace.get("mode", "direct")

    result: dict[str, Any] = {"provider": provider, "mode": mode}

    if provider == "google_api":
        try:
            from workspace_client import get_workspace_client
            client = get_workspace_client(config)
            healthy = client.health_check()
            result["healthy"] = healthy
            result["class"] = client.__class__.__name__
            google = config.get("google", {})
            result["delegate_email"] = google.get("delegate_email", "")
            result["account_alias"] = google.get("account_alias", "")
        except Exception as exc:
            result["healthy"] = False
            result["error"] = str(exc)
    elif provider == "composio":
        result["healthy"] = False
        result["note"] = "Composio backend not yet implemented"
        result["api_key_set"] = bool(os.getenv("COMPOSIO_API_KEY"))
    else:
        result["healthy"] = False
        result["error"] = f"Unknown provider: {provider}"

    print(json.dumps(result, indent=2))
    return 0 if result.get("healthy") else 1


def cmd_provider_google_api(config: dict[str, Any]) -> int:
    """Verify google_api provider setup."""
    google = config.get("google", {}) if isinstance(config, Mapping) else {}
    sa_path = google.get("service_account_path", "")

    print("=== Google API Provider Setup ===\n")

    # Check google_api.py
    try:
        from providers.google_workspace import _find_google_api_script
        script = _find_google_api_script()
        print(f"✅ google_api.py found: {script}")
    except FileNotFoundError as exc:
        print(f"❌ {exc}")
        print("\nNext steps:")
        print("  1. Install the google-workspace skill for Hermes")
        return 1

    # Check service account
    if sa_path:
        sa = Path(str(sa_path)).expanduser()
        if sa.exists():
            print(f"✅ Service account found: {sa}")
        else:
            print(f"❌ Service account not found at: {sa}")
            print("\nNext steps:")
            print("  1. Create a service account in Google Cloud Console")
            print("  2. Download the JSON key file")
            print(f"  3. Place it at: {sa}")
            return 1
    else:
        print("⚠️  No service_account_path in config — using OAuth mode")

    # Check delegate email
    delegate = google.get("delegate_email", "")
    if delegate:
        print(f"✅ Delegate email: {delegate}")
    else:
        print("⚠️  No delegate_email in config")

    # Test auth
    try:
        from workspace_client import get_workspace_client
        client = get_workspace_client(config)
        healthy = client.health_check()
        if healthy:
            print("\n✅ Auth test passed — calendar list succeeded")
            return 0
        else:
            print("\n❌ Auth test failed — calendar list returned error")
            print("\nNext steps:")
            print("  1. Verify service account has domain-wide delegation enabled")
            print("  2. Verify scopes are authorized in Google Workspace Admin Console")
            return 1
    except Exception as exc:
        print(f"\n❌ Auth test error: {exc}")
        return 1


def cmd_provider_composio(config: dict[str, Any], print_steps: bool) -> int:
    """Print Composio onboarding steps (stub)."""
    print("=== Composio Provider Setup ===\n")
    print("⚠️  Composio backend coming in v0.1.5\n")

    api_key = os.getenv("COMPOSIO_API_KEY")
    if api_key:
        print(f"✅ COMPOSIO_API_KEY is set")
    else:
        print("❌ COMPOSIO_API_KEY not set")

    if print_steps or True:
        print("\nNext steps to enable Composio:")
        print("  1. Install Composio SDK: pip install composio-core")
        print("  2. Set COMPOSIO_API_KEY in .env (get one at https://composio.dev)")
        print("  3. Update company.yaml:")
        print("     integrations:")
        print("       workspace:")
        print("         provider: composio")
        print("         mode: sdk")
        print("         user_id: \"your-user-id\"")
        print("  4. Run: python connect_workspace.py --provider composio --connect")
        print("  5. Authenticate Google account via Composio")
        print("\nNote: The --connect flag will be implemented in v0.1.5")

    return 0


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Workspace provider onboarding and status"
    )
    parser.add_argument("--config", help="Path to company.yaml")
    parser.add_argument("--status", action="store_true", help="Print current provider status")
    parser.add_argument("--provider", choices=["google_api", "composio"],
                        help="Verify or set up a specific provider")
    parser.add_argument("--print-next-steps", action="store_true",
                        help="Print next steps for the selected provider")
    args = parser.parse_args()

    config = _load_config(args.config)

    if args.status:
        return cmd_status(config)
    elif args.provider == "google_api":
        return cmd_provider_google_api(config)
    elif args.provider == "composio":
        return cmd_provider_composio(config, args.print_next_steps)
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(_main())