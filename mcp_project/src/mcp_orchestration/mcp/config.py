# """Which MCP servers the agent connects to at the start of each chat run.

# Defaults to the bundled demo server (no external dependencies, no API keys)
# so the project runs out of the box. To wire up real connectors, either:

# 1. Set MCP_SERVERS_CONFIG in the environment to the path of a JSON file
#    shaped like:
#        [
#          {"name": "filesystem", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/some/dir"]},
#          {"name": "demo", "command": "python", "args": ["-m", "mcp_orchestration.mcp.demo_server"]}
#        ]
# 2. Or just edit DEFAULT_SERVERS below.

# The bundled demo server is the only default connector. External connectors
# should be enabled explicitly through `MCP_SERVERS_CONFIG` after their
# credentials and runtime prerequisites are configured.
# """

# import json
# import logging
# import os
# import sys
# from dataclasses import dataclass
# from pathlib import Path

# logger = logging.getLogger("mcp_orchestration.mcp.config")


# @dataclass(frozen=True)
# class MCPServerConfig:
#     name: str
#     command: str
#     args: list[str]
#     env: dict[str, str] | None = None


# def _demo_server_config() -> MCPServerConfig:
#     """The bundled demo server, launched with the same interpreter running
#     the API so it works identically in dev, Docker, or CI without assuming
#     `uv` (or anything else) is on PATH."""
#     return MCPServerConfig(
#         name="demo",
#         command=sys.executable,
#         args=["-m", "mcp_orchestration.mcp.demo_server"],
#     )


# def _gmail_server_config() -> MCPServerConfig:
#     """Gmail MCP server (npx package, launched over stdio like any other
#     server here). Needs Node.js + npx on PATH. First run triggers a Google
#     OAuth flow in the browser; credentials are cached locally afterwards.
#     See GMAIL_* vars"""
#     return MCPServerConfig(
#         name="gmail",
#         command="npx",
#         args=["-y", "@gongrzhe/server-gmail-autoauth-mcp"],
#         env={
#             "GMAIL_CLIENT_ID": os.getenv("GMAIL_CLIENT_ID", ""),
#             "GMAIL_CLIENT_SECRET": os.getenv("GMAIL_CLIENT_SECRET", ""),
#         },
#     )


# # def _email_server_config() -> MCPServerConfig:
# #     """Generic SMTP/IMAP email MCP server (npx package) for sending/reading
# #     mail on accounts that aren't Gmail (or as a Gmail alternative via app
# #     password). Needs EMAIL_* vars in .env.example."""
# #     return MCPServerConfig(
# #         name="email",
# #         command="npx",
# #         args=["-y", "@modelcontextprotocol/server-email"],
# #         env={
# #             "EMAIL_HOST": os.getenv("EMAIL_HOST", ""),
# #             "EMAIL_PORT": os.getenv("EMAIL_PORT", "587"),
# #             "EMAIL_USER": os.getenv("EMAIL_USER", ""),
# #             "EMAIL_PASSWORD": os.getenv("EMAIL_PASSWORD", ""),
# #         },
# #     )


# DEFAULT_SERVERS: list[MCPServerConfig] = [_demo_server_config(), _gmail_server_config()]
# # DEFAULT_SERVERS: list[MCPServerConfig] = [_gmail_server_config()]


# def load_mcp_server_configs() -> list[MCPServerConfig]:
#     """Load MCP server definitions from MCP_SERVERS_CONFIG if set, else defaults."""
#     config_path = os.getenv("MCP_SERVERS_CONFIG")
#     if not config_path:
#         return DEFAULT_SERVERS

#     path = Path(config_path)
#     if not path.exists():
#         logger.warning(
#             f"MCP_SERVERS_CONFIG points at '{config_path}' which does not exist; using defaults."
#         )
#         return DEFAULT_SERVERS

#     try:
#         raw = json.loads(path.read_text())
#         return [
#             MCPServerConfig(
#                 name=s["name"],
#                 command=s["command"],
#                 args=s.get("args", []),
#                 env=s.get("env"),
#             )
#             for s in raw
#         ]
#     except (json.JSONDecodeError, KeyError, OSError) as e:
#         logger.error(
#             f"Failed to parse MCP_SERVERS_CONFIG '{config_path}': {e}. Using defaults."
#         )
#         return DEFAULT_SERVERS

"""Which MCP servers the agent connects to at the start of each chat run.

Gmail is now served by our OWN custom MCP server (gmail_mcp_server.py),
launched over stdio, talking to the standard/generally-available Gmail API.
This replaces the old dependency on Google's Workspace Developer Preview
"Gmail MCP" HTTP endpoint (gmailmcp.googleapis.com), which required special
enrollment and was the source of the earlier 404s.

    AI Agent -> MCPClientManager (stdio) -> gmail_mcp_server.py -> Gmail API

To wire up additional connectors, either:

1. Set MCP_SERVERS_CONFIG in the environment to the path of a JSON file
   shaped like:
       [
         {"name": "filesystem", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/some/dir"]},
         {"name": "demo", "command": "python", "args": ["-m", "mcp_orchestration.mcp.demo_server"]}
       ]
2. Or just edit DEFAULT_SERVERS below.
"""

import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("mcp_orchestration.mcp.config")


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    command: Optional[str] = None  # For stdio servers
    args: Optional[list[str]] = None  # For stdio servers
    env: Optional[dict[str, str]] = None
    cwd: Optional[str] = None
    enabled: bool = True
    # Kept for backward compatibility with any code still branching on
    # `.transport` — everything here is stdio now, but the field remains so
    # client.py's `connect_server()` dispatch (which checks
    # `config.transport == "http"`) doesn't need to change.
    url: Optional[str] = None
    transport: str = "stdio"
    auth_type: Optional[str] = None


def _demo_server_config() -> MCPServerConfig:
    """The bundled demo server."""
    return MCPServerConfig(
        name="demo",
        command=sys.executable,
        args=["-m", "mcp_orchestration.mcp.demo_server"],
        transport="stdio",
    )


def _gmail_server_config(user_id: Optional[str]) -> Optional[MCPServerConfig]:
    """Our custom Gmail MCP server (gmail_mcp_server.py), launched over
    stdio with the same Python interpreter running the API.

    Auth is handled by the EXISTING /auth/gmail/login -> /auth/gmail/callback
    flow (gmail_oauth.py), which persists credentials per-user in the
    `gmail_credentials` Mongo collection instead of a shared local file (see
    repositories/gmail_credentials.py). Since this MCP server runs as a
    separate subprocess, it can't share the FastAPI app's DB connection or
    know which user it's acting for on its own — both are passed in via env:
    `GOOGLE_USER_ID` (whose credentials to load) and `MONGO_URI`/`DB_NAME`/
    `TOKEN_ENCRYPTION_KEY` (how to read and decrypt them). No `user_id` means
    no Gmail access for this connection — there's no shared account to fall
    back to anymore.
    """
    if not user_id:
        logger.info("No user_id for this session; Gmail MCP server disabled.")
        return None

    client_id = os.getenv("GOOGLE_CLIENT_ID") or os.getenv("GMAIL_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET") or os.getenv(
        "GMAIL_CLIENT_SECRET"
    )
    encryption_key = os.getenv("TOKEN_ENCRYPTION_KEY")

    if not client_id or not client_secret or not encryption_key:
        logger.warning(
            "Missing Google OAuth credentials (GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET "
            "or legacy GMAIL_CLIENT_ID/GMAIL_CLIENT_SECRET) or TOKEN_ENCRYPTION_KEY. "
            "Gmail MCP server disabled."
        )
        return None

    env = {
        "GOOGLE_CLIENT_ID": client_id,
        "GOOGLE_CLIENT_SECRET": client_secret,
        "GOOGLE_REDIRECT_URI": os.getenv("GOOGLE_REDIRECT_URI")
        or os.getenv("GMAIL_REDIRECT_URI", "http://localhost:8000/auth/gmail/callback"),
        "GOOGLE_USER_ID": user_id,
        "MONGO_URI": os.getenv("MONGO_URI", "mongodb://localhost:27017"),
        "DB_NAME": os.getenv("DB_NAME", "mcp_orchestration"),
        "TOKEN_ENCRYPTION_KEY": encryption_key,
    }

    return MCPServerConfig(
        name="gmail",
        command=sys.executable,
        args=["-m", "mcp_orchestration.mcp.gmail_mcp_server"],
        env=env,
        transport="stdio",
        enabled=True,
    )


def load_mcp_server_configs(user_id: Optional[str] = None) -> list[MCPServerConfig]:
    """Load MCP server definitions from MCP_SERVERS_CONFIG if set, else defaults.

    `user_id` identifies whose Gmail credentials the Gmail MCP server (if
    enabled) should act as for this connection — see `_gmail_server_config`.
    """
    config_path = os.getenv("MCP_SERVERS_CONFIG")

    configs: list[MCPServerConfig] = []

    gmail_config = _gmail_server_config(user_id)
    if gmail_config and gmail_config.enabled:
        configs.append(gmail_config)
        logger.info("✅ Gmail MCP (custom, stdio) server enabled")
    else:
        logger.info("ℹ️ Gmail MCP server disabled (missing OAuth credentials)")

    configs.append(_demo_server_config())
    logger.debug("✅ Demo MCP server enabled")

    if config_path:
        path = Path(config_path)
        if path.exists():
            try:
                raw = json.loads(path.read_text())
                for s in raw:
                    name = s.get("name")
                    if name == "gmail":
                        # Allow full override, but default anything unset to
                        # our custom server + env so partial overrides
                        # (e.g. just changing args) still work.
                        base = gmail_config
                        merged_env = {**(base.env if base else {}), **s.get("env", {})}
                        configs = [c for c in configs if c.name != "gmail"]
                        configs.append(
                            MCPServerConfig(
                                name="gmail",
                                command=s.get("command", sys.executable),
                                args=s.get(
                                    "args",
                                    ["-m", "mcp_orchestration.mcp.gmail_mcp_server"],
                                ),
                                env=merged_env,
                                cwd=s.get("cwd"),
                                transport="stdio",
                                enabled=s.get("enabled", True),
                            )
                        )
                    else:
                        configs.append(
                            MCPServerConfig(
                                name=name,
                                command=s["command"],
                                args=s.get("args", []),
                                env=s.get("env"),
                                cwd=s.get("cwd"),
                                transport="stdio",
                                enabled=s.get("enabled", True),
                            )
                        )
                logger.info(
                    f"Loaded {len(configs)} MCP server configurations from {config_path}"
                )
            except Exception as e:
                logger.error(f"Failed to parse MCP_SERVERS_CONFIG: {e}")

    return configs


def get_server_config(name: str) -> Optional[MCPServerConfig]:
    """Get configuration for a specific MCP server by name."""
    configs = load_mcp_server_configs()
    return next((c for c in configs if c.name == name and c.enabled), None)


def is_server_enabled(name: str) -> bool:
    """Check if a specific MCP server is enabled."""
    return get_server_config(name) is not None
