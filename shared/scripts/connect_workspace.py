#!/usr/bin/env python3
"""Onboarding script for workspace provider setup.

Usage:
    python connect_workspace.py --status
    python connect_workspace.py --provider google_api
    python connect_workspace.py --provider composio --print-next-steps
    python connect_workspace.py --provider composio --connect gmail
    python connect_workspace.py --provider composio --connect googlecalendar
    python connect_workspace.py --provider composio --mcp-url
    python connect_workspace.py --provider composio --mcp-info
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
        result["mcp_key_set"] = bool(os.getenv("COMPOSIO_MCP_KEY"))
        result["user_id"] = workspace.get("user_id", "")
        # Check connections
        try:
            from providers.composio_workspace import load_session_meta, ComposioWorkspaceClient
            meta = load_session_meta(config)
            if meta:
                result["connections"] = meta.get("connections", {})
            else:
                result["connections"] = {}
            # Try health check + refresh connection statuses
            try:
                client = ComposioWorkspaceClient(config)
                result["healthy"] = client.health_check()
                # Refresh actual connection state from Composio
                refreshed = client.refresh_connection_statuses()
                result["connections"] = {
                    tk: {"status": status} for tk, status in refreshed.items()
                }
            except Exception as exc:
                result["healthy"] = False
                result["error"] = str(exc)
        except ImportError:
            result["healthy"] = False
            result["error"] = "composio package not installed"
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

    try:
        from providers.google_workspace import _find_google_api_script
        script = _find_google_api_script()
        print(f"✅ google_api.py found: {script}")
    except FileNotFoundError as exc:
        print(f"❌ {exc}")
        print("\nNext steps:")
        print("  1. Install the google-workspace skill for Hermes")
        return 1

    if sa_path:
        sa = Path(str(sa_path)).expanduser()
        if sa.exists():
            print(f"✅ Service account found: {sa}")
        else:
            print(f"❌ Service account not found at: {sa}")
            return 1
    else:
        print("⚠️  No service_account_path in config — using OAuth mode")

    delegate = google.get("delegate_email", "")
    if delegate:
        print(f"✅ Delegate email: {delegate}")

    try:
        from workspace_client import get_workspace_client
        client = get_workspace_client(config)
        healthy = client.health_check()
        if healthy:
            print("\n✅ Auth test passed — calendar list succeeded")
            return 0
        else:
            print("\n❌ Auth test failed")
            return 1
    except Exception as exc:
        print(f"\n❌ Auth test error: {exc}")
        return 1


def cmd_composio_connect(config: dict[str, Any], toolkit: str) -> int:
    """Connect a Composio toolkit via MCP COMPOSIO_MANAGE_CONNECTIONS."""
    print(f"=== Composio Connect: {toolkit} ===\n")
    return _cmd_composio_connect_mcp(config, toolkit)


def _cmd_composio_connect_mcp(config: dict[str, Any], toolkit: str) -> int:
    """Connect via MCP COMPOSIO_MANAGE_CONNECTIONS."""
    mcp_cfg = config.get("integrations", {}).get("workspace", {}).get("mcp", {})
    key_env = mcp_cfg.get("key_env", "COMPOSIO_MCP_KEY")

    if not os.getenv(key_env):
        print(f"❌ {key_env} not set")
        print(f"   Get a Composio MCP key and set it in your .env file")
        return 1

    try:
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient, save_session_meta, load_session_meta
    except ImportError as exc:
        print(f"❌ {exc}")
        return 1

    try:
        client = ComposioMCPWorkspaceClient(config)
        result = client._manage_connections("connect", toolkit)

        # Extract redirect URL and connection info
        results = result.get("results", {})
        tk_info = results.get(toolkit, {})
        redirect_url = tk_info.get("redirect_url", "")
        accounts = tk_info.get("accounts", [])

        if redirect_url:
            print(f"\n👉 Connect Link:")
            print(f"   {redirect_url}")
            print(f"\nOpen this URL in your browser to connect {toolkit}.")
        else:
            print(f"\n⚠️  No redirect URL returned")

        if accounts:
            print(f"\nExisting accounts:")
            for acc in accounts:
                status = acc.get("status", "unknown")
                icon = "✅" if status == "active" else "⏳"
                print(f"  {icon} {acc.get('id', '?')} — {status}")

        # Update session metadata
        meta = load_session_meta(config) or {
            "provider": "composio",
            "mode": "mcp",
            "endpoint": client.endpoint,
            "key_env": key_env,
            "mcp_initialized": True,
            "available_meta_tools": ["COMPOSIO_MANAGE_CONNECTIONS", "COMPOSIO_MULTI_EXECUTE_TOOL"],
            "connections": {},
        }
        meta["connections"][toolkit] = {"status": "pending"}
        save_session_meta(config, meta)

        return 0

    except ValueError as exc:
        print(f"❌ Config error: {exc}")
        return 1
    except Exception as exc:
        print(f"❌ Connection failed: {exc}")
        return 1


def cmd_composio_connections(config: dict[str, Any]) -> int:
    """Show connection status for all toolkits."""
    print("=== Composio Connections ===\n")

    try:
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = ComposioMCPWorkspaceClient(config)
        statuses = client.refresh_connection_statuses()

        for tk, status in statuses.items():
            icon = "✅" if status == "connected" else "⚠️"
            print(f"  {icon} {tk}: {status}")
        return 0
    except Exception as exc:
        print(f"❌ {exc}")
        return 1


def cmd_composio_test(config: dict[str, Any], toolkit: str) -> int:
    """Run a live test against a toolkit."""
    print(f"=== Composio Test: {toolkit} ===\n")
    mode = config.get("integrations", {}).get("workspace", {}).get("mode", "mcp")

    try:
        from workspace_client import get_workspace_client
        client = get_workspace_client(config)

        if toolkit == "gmail":
            results = client.gmail_search("is:unread", max_results=3)
            print(f"✅ Gmail: got {len(results)} unread emails")
            for e in results[:3]:
                subject = e.get("subject", e.get("Subject", "?"))[:60]
                print(f"   {subject}")
        elif toolkit in ("googlecalendar", "calendar"):
            from datetime import date, timedelta
            start = date.today().isoformat()
            end = (date.today() + timedelta(days=7)).isoformat()
            results = client.calendar_list(start, end)
            print(f"✅ Calendar: got {len(results)} events in next 7 days")
        elif toolkit in ("googledrive", "drive"):
            results = client.drive_search("", max_results=5)
            print(f"✅ Drive: got {len(results)} files")
            for f in results[:5]:
                name = f.get("name", "?")[:60]
                print(f"   {name}")
        else:
            print(f"❌ Unknown toolkit: {toolkit}")
            return 1

        return 0
    except Exception as exc:
        print(f"❌ Test failed: {exc}")
        return 1


def cmd_composio_mcp_url(config: dict[str, Any]) -> int:
    """Print the MCP endpoint URL from config (no session required)."""
    print("=== Composio MCP Endpoint ===\n")

    workspace = config.get("integrations", {}).get("workspace", {})
    mcp_cfg = workspace.get("mcp", {})
    endpoint = mcp_cfg.get("endpoint", "https://connect.composio.dev/mcp")
    key_env = mcp_cfg.get("key_env", "COMPOSIO_MCP_KEY")

    print(f"Endpoint: {endpoint}")
    print(f"Key env:  {key_env}")
    print(f"Key set:  {'✅' if os.getenv(key_env) else '❌'}")

    if os.getenv(key_env):
        try:
            sys.path.insert(0, str(_SCRIPT_DIR))
            from mcp_client import MCPClient
            client = MCPClient(endpoint=endpoint, key_env=key_env)
            client.initialize()
            print(f"Initialized: ✅ (session: {client.session_id})")
            return 0
        except Exception as exc:
            print(f"Initialized: ❌ ({exc})")
            return 1
    return 1


def cmd_provider_composio(config: dict[str, Any], print_steps: bool) -> int:
    """Print Composio onboarding info — MCP mode only."""
    integrations = config.get("integrations", {})
    workspace = integrations.get("workspace", {})
    mode = workspace.get("mode", "mcp")

    print("=== Composio Provider Setup ===\n")

    if mode == "sdk":
        print("❌ SDK mode was removed in v0.1.9.\n")
        print("Migration:")
        print("  1. Change mode to 'mcp' in company.yaml")
        print("  2. Set COMPOSIO_MCP_KEY in .env")
        print("  3. Run: python connect_workspace.py --provider composio --connections")
        return 1

    mcp_cfg = workspace.get("mcp", {})
    endpoint = mcp_cfg.get("endpoint", "https://connect.composio.dev/mcp")
    key_env = mcp_cfg.get("key_env", "COMPOSIO_MCP_KEY")

    print(f"✅ Mode: mcp")
    print(f"✅ Endpoint: {endpoint}")
    if os.getenv(key_env):
        print(f"✅ {key_env}: set")
    else:
        print(f"❌ {key_env}: NOT set")

    user_id = workspace.get("user_id", "")
    if user_id:
        print(f"✅ user_id: {user_id}")
    else:
        print("⚠️  user_id not set in config")

    # Show meta tools if initialized
    if os.getenv(key_env):
        try:
            sys.path.insert(0, str(_SCRIPT_DIR))
            from mcp_client import MCPClient
            client = MCPClient(endpoint=endpoint, key_env=key_env)
            client.initialize()
            tools = client.list_tools()
            tool_names = [t.get("name", "?") for t in tools]
            print(f"✅ MCP initialized: {len(tools)} meta tools")
            for name in tool_names:
                print(f"   - {name}")
        except Exception as exc:
            print(f"⚠️  MCP initialize failed: {exc}")

    if print_steps or not os.getenv(key_env):
        print("\nNext steps:")
        print(f"  1. Set {key_env} in .env (from https://connect.composio.dev)")
        print("  2. Set integrations.workspace.user_id in company.yaml")
        print("  3. python connect_workspace.py --provider composio --connect gmail")
        print("  4. python connect_workspace.py --provider composio --connect googlecalendar")
        print("  5. python connect_workspace.py --provider composio --connect googledrive")
        print("  6. python connect_workspace.py --status")

    return 0 if os.getenv(key_env) else 1


def cmd_composio_mcp_info(config: dict[str, Any], json_output: bool = False, tools_only: bool = False) -> int:
    """Print MCP endpoint info — works for MCP mode without SDK session_id."""
    integrations = config.get("integrations", {})
    workspace = integrations.get("workspace", {})
    mode = workspace.get("mode", "mcp")
    mcp_cfg = workspace.get("mcp", {})
    endpoint = mcp_cfg.get("endpoint", "https://connect.composio.dev/mcp")
    key_env = mcp_cfg.get("key_env", "COMPOSIO_MCP_KEY")

    # Get enabled tools from config
    try:
        from providers.composio_mcp_workspace import get_enabled_tools
    except ImportError as exc:
        print(f"❌ {exc}")
        return 1

    read_tools = get_enabled_tools(config, "read")
    write_tools = get_enabled_tools(config, "write_safe")
    all_tools: dict[str, list[str]] = {}
    for tk in set(list(read_tools.keys()) + list(write_tools.keys())):
        all_tools[tk] = read_tools.get(tk, []) + write_tools.get(tk, [])

    # Try to get MCP meta tools and initialization status
    mcp_initialized = False
    available_meta_tools: list[str] = []
    if os.getenv(key_env):
        try:
            sys.path.insert(0, str(_SCRIPT_DIR))
            from mcp_client import MCPClient
            client = MCPClient(endpoint=endpoint, key_env=key_env)
            client.initialize()
            mcp_initialized = True
            tools = client.list_tools()
            available_meta_tools = [t.get("name", "?") for t in tools]
        except Exception:
            pass

    # Load session metadata for connections
    try:
        from providers.composio_mcp_workspace import load_session_meta
        meta = load_session_meta(config) or {}
    except Exception:
        meta = {}

    result = {
        "provider": "composio",
        "mode": mode,
        "endpoint": endpoint,
        "key_env": key_env,
        "mcp_initialized": mcp_initialized,
        "available_meta_tools": available_meta_tools,
        "enabled_tools": all_tools,
        "headers_stored": meta.get("mcp", {}).get("headers_stored", False),
        "connections": meta.get("connections", {}),
    }

    if tools_only:
        for tk, tools in all_tools.items():
            print(f"{tk}: {', '.join(tools) if tools else '(none)'}")
    elif json_output:
        print(json.dumps(result, indent=2))
    else:
        print(f"Provider: {result['provider']}")
        print(f"Mode:     {result['mode']}")
        print(f"Endpoint: {result['endpoint']}")
        print(f"Key env:  {result['key_env']}")
        print(f"MCP init: {'✅' if result['mcp_initialized'] else '❌'}")
        if available_meta_tools:
            print(f"Meta tools: {', '.join(available_meta_tools)}")
        print(f"\nEnabled tools:")
        for tk, tools in all_tools.items():
            print(f"  {tk}: {', '.join(tools) if tools else '(none)'}")
        connections = result.get("connections", {})
        if connections:
            print(f"\nConnections:")
            for tk, info in connections.items():
                status = info.get("status", "unknown")
                icon = "✅" if status == "connected" else "⚠️"
                print(f"  {icon} {tk}: {status}")

    return 0 if mcp_initialized else 1


def cmd_composio_debug_tool(config: dict[str, Any], toolkit: str) -> int:
    """Debug: test MCP meta-tools with full raw output."""
    print(f"=== Composio Debug: {toolkit} ===\n")
    import json as _json

    tool_map = {
        "gmail": [("GMAIL_FETCH_EMAILS", {"max_results": 2})],
        "googlecalendar": [("GOOGLECALENDAR_FIND_EVENT", {
            "time_min": "2026-01-01T00:00:00Z",
            "time_max": "2026-12-31T23:59:59Z",
            "max_results": 2,
        })],
        "googledrive": [("GOOGLEDRIVE_FIND_FILE", {"query": "", "max_results": 3})],
    }

    if toolkit not in tool_map:
        print(f"❌ Unknown toolkit: {toolkit}. Use: gmail, googlecalendar, googledrive")
        return 1

    try:
        from workspace_client import get_workspace_client
        from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
        client = get_workspace_client(config)
        if not isinstance(client, ComposioMCPWorkspaceClient):
            print("❌ Debug tool only works in MCP mode")
            return 1

        mcp = client._get_mcp()
        mcp.initialize()
        print(f"✅ MCP session: {mcp.session_id}\n")

        # List meta tools
        tools = mcp.list_tools()
        print(f"Meta tools ({len(tools)}):")
        for t in tools:
            print(f"  {t.get('name', '?')}")
        print()

        # Test each tool slug
        for tool_slug, input_data in tool_map[toolkit]:
            print(f"--- {tool_slug} ---")
            print(f"Payload: {_json.dumps({'tools': [{'tool_slug': tool_slug, 'arguments': input_data}]}, indent=2)}")
            try:
                result = client._execute_composio_tool(tool_slug, input_data)
                # Show keys and summary
                if isinstance(result, dict):
                    print(f"Response keys: {list(result.keys())[:10]}")
                    # Show normalized result
                    normalized = client._normalize_tool_result(tool_slug, result)
                    if isinstance(normalized, list):
                        print(f"Normalized: {len(normalized)} items")
                        if normalized:
                            print(f"First item keys: {list(normalized[0].keys())[:8]}")
                    else:
                        print(f"Normalized: {type(normalized).__name__}")
                else:
                    print(f"Response type: {type(result)}")
            except Exception as exc:
                print(f"Error: {exc}")
            print()

        return 0
    except Exception as exc:
        print(f"❌ Debug failed: {exc}")
        return 1


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
    parser.add_argument("--connect", metavar="TOOLKIT",
                        help="Connect a Composio toolkit (e.g. gmail, googlecalendar)")
    parser.add_argument("--mcp-url", action="store_true",
                        help="Print MCP endpoint URL for Composio session")
    parser.add_argument("--mcp-info", action="store_true",
                        help="Print detailed MCP endpoint info (URL, tools, status)")
    parser.add_argument("--mcp-tools", action="store_true",
                        help="Print enabled tools for MCP session")
    parser.add_argument("--connections", action="store_true",
                        help="Show Composio connection status for all toolkits")
    parser.add_argument("--tools", action="store_true",
                        help="List available MCP tools")
    parser.add_argument("--test", metavar="TOOLKIT",
                        help="Run a live test against a toolkit (gmail, googlecalendar, googledrive)")
    parser.add_argument("--debug-tool", metavar="TOOLKIT",
                        help="Debug: test all MCP meta-tools for a toolkit with full output")
    args = parser.parse_args()

    config = _load_config(args.config)

    if args.status:
        return cmd_status(config)
    elif args.provider == "google_api":
        return cmd_provider_google_api(config)
    elif args.provider == "composio" and args.connect:
        return cmd_composio_connect(config, args.connect)
    elif args.provider == "composio" and args.mcp_url:
        return cmd_composio_mcp_url(config)
    elif args.provider == "composio" and (args.mcp_info or args.mcp_tools):
        return cmd_composio_mcp_info(config, json_output=bool(args.mcp_info), tools_only=bool(args.mcp_tools))
    elif args.provider == "composio" and args.connections:
        return cmd_composio_connections(config)
    elif args.provider == "composio" and args.tools:
        # List MCP meta tools
        try:
            from providers.composio_mcp_workspace import ComposioMCPWorkspaceClient
            client = ComposioMCPWorkspaceClient(config)
            mcp = client._get_mcp()
            tools = mcp.list_tools()
            print(f"MCP tools ({len(tools)}):")
            for t in tools:
                print(f"  {t.get('name', '?')}: {t.get('description', '')[:60]}")
            return 0
        except Exception as exc:
            print(f"❌ {exc}")
            return 1
    elif args.provider == "composio" and args.test:
        return cmd_composio_test(config, args.test)
    elif args.provider == "composio" and args.debug_tool:
        return cmd_composio_debug_tool(config, args.debug_tool)
    elif args.provider == "composio":
        return cmd_provider_composio(config, args.print_next_steps)
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(_main())