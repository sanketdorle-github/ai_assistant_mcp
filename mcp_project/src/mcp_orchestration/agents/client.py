"""MCP Client Manager for connecting to and managing MCP servers."""

import asyncio
import json
import logging
import os
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import Tool

from mcp_orchestration.mcp.config import get_server_config, load_mcp_server_configs

logger = logging.getLogger("mcp_orchestration.agents.client")


class MCPClientManager:
    """Manages connections to MCP servers and provides a unified interface for tools.

    Both stdio and HTTP (streamable-http) servers end up as a plain
    `ClientSession` in `self.sessions` once connected — there is no more
    separate hand-rolled REST path for HTTP servers. This means
    `list_tools`, `execute_tool`, and `close_all` no longer need to branch
    on transport type at all.
    """

    def __init__(self):
        self.sessions: Dict[str, ClientSession] = {}
        self.tool_cache: Dict[str, List[Tool]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._access_tokens: Dict[str, str] = {}
        # Each connected server (stdio or http) owns an AsyncExitStack that
        # holds its transport + session context managers open for the
        # lifetime of the connection, so we can close them cleanly later.
        self._stacks: Dict[str, AsyncExitStack] = {}

    async def connect_http_server(
        self,
        name: str,
        url: str,
        auth_type: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> bool:
        """Connect to a remote MCP server over Streamable HTTP.

        Uses the official `mcp` SDK's `streamablehttp_client`, which speaks
        proper JSON-RPC 2.0 over the MCP Streamable HTTP transport
        (handshake, session id headers, SSE responses, etc.) instead of a
        hand-rolled REST shape. This is required for real remote MCP
        servers such as Google's Gmail MCP server
        (https://gmailmcp.googleapis.com/mcp/v1), which do not expose a
        `POST /` or `POST /tools/call` REST API.
        """
        stack = AsyncExitStack()
        try:
            logger.info(f"Connecting to HTTP MCP server: {name} at {url}")

            headers = {}

            # Get OAuth token for Gmail
            if auth_type == "oauth2" and name == "gmail":
                token = await self._get_gmail_oauth_token(env)
                if not token:
                    logger.error("Failed to get Gmail OAuth token")
                    return False
                self._access_tokens[name] = token
                headers["Authorization"] = f"Bearer {token}"
                logger.info("✅ Gmail OAuth token obtained")

            read_stream, write_stream, _get_session_id = (
                await stack.enter_async_context(
                    streamablehttp_client(url, headers=headers)
                )
            )
            session = await stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )

            async with asyncio.timeout(30):
                await session.initialize()
                tools_result = await session.list_tools()

            self.sessions[name] = session
            self.tool_cache[name] = tools_result.tools
            self._locks[name] = asyncio.Lock()
            self._stacks[name] = stack

            logger.info(
                f"✅ Connected to HTTP MCP server '{name}' with {len(tools_result.tools)} tools"
            )
            tool_names = [t.name for t in tools_result.tools]
            if tool_names:
                logger.info(f"   Available tools: {', '.join(tool_names)}")
            return True

        except Exception as e:
            logger.error(
                f"Failed to connect to HTTP MCP server '{name}': {e}", exc_info=True
            )
            # Clean up anything that was opened before the failure.
            await stack.aclose()
            return False

    async def _get_gmail_oauth_token(
        self, env: Optional[Dict[str, str]] = None
    ) -> Optional[str]:
        """Get Gmail OAuth token from stored credentials or refresh."""
        token_file = None
        client_id = None
        client_secret = None

        if env:
            token_file = env.get("GMAIL_TOKEN_FILE")
            client_id = env.get("GMAIL_CLIENT_ID")
            client_secret = env.get("GMAIL_CLIENT_SECRET")

        # Try to read existing token
        if token_file and Path(token_file).exists():
            try:
                with open(token_file, "r") as f:
                    tokens = json.load(f)
                access_token = tokens.get("access_token")
                expires_in = tokens.get("expires_in", 0)

                # Check if token is still valid (with 5 min buffer)
                if access_token and expires_in > 60:
                    logger.info("✅ Using cached Gmail access token")
                    return access_token

                # Try to refresh token
                refresh_token = tokens.get("refresh_token")
                if refresh_token and client_id and client_secret:
                    logger.info("Refreshing Gmail access token...")
                    new_token = await self._refresh_gmail_token(
                        refresh_token, client_id, client_secret, token_file
                    )
                    if new_token:
                        return new_token
            except Exception as e:
                logger.warning(f"Failed to read token file: {e}")

        # If no valid token, we need to trigger OAuth flow
        logger.warning(
            "No valid Gmail token found. Please authenticate at /auth/gmail/login"
        )
        return None

    async def _refresh_gmail_token(
        self, refresh_token: str, client_id: str, client_secret: str, token_file: str
    ) -> Optional[str]:
        """Refresh Gmail access token using refresh token."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    "https://oauth2.googleapis.com/token",
                    data={
                        "refresh_token": refresh_token,
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "grant_type": "refresh_token",
                    },
                    timeout=30.0,
                )
                response.raise_for_status()
                tokens = response.json()

                # Update token file
                if token_file and Path(token_file).exists():
                    with open(token_file, "r") as f:
                        existing = json.load(f)
                    existing.update(
                        {
                            "access_token": tokens["access_token"],
                            "expires_in": tokens.get("expires_in", 3600),
                        }
                    )
                    with open(token_file, "w") as f:
                        json.dump(existing, f, indent=2)

                return tokens["access_token"]
        except Exception as e:
            logger.error(f"Failed to refresh token: {e}")
            return None

    async def connect_stdio_server(
        self,
        name: str,
        command: str,
        args: List[str],
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
    ) -> bool:
        """Connect to a stdio-based MCP server."""
        stack = AsyncExitStack()
        try:
            server_env = os.environ.copy()
            if env:
                server_env.update(env)

            logger.info(f"Connecting to stdio MCP server: {name}")
            logger.debug(f"Command: {command} {' '.join(args)}")

            server_params = StdioServerParameters(
                command=command,
                args=args,
                env=server_env,
                cwd=cwd,
            )

            async with asyncio.timeout(60):  # Longer timeout for OAuth
                read_stream, write_stream = await stack.enter_async_context(
                    stdio_client(server_params)
                )
                session = await stack.enter_async_context(
                    ClientSession(read_stream, write_stream)
                )
                await session.initialize()

                self.sessions[name] = session
                self._locks[name] = asyncio.Lock()

                tools = await session.list_tools()
                self.tool_cache[name] = tools.tools
                self._stacks[name] = stack

                logger.info(
                    f"✅ Connected to stdio MCP server '{name}' with {len(tools.tools)} tools"
                )
                return True

        except Exception as e:
            logger.error(
                f"Failed to connect to stdio server '{name}': {e}", exc_info=True
            )
            await stack.aclose()
            return False

    async def connect_server(self, config) -> bool:
        """Connect to an MCP server based on its transport type."""
        if config.transport == "http":
            return await self.connect_http_server(
                config.name,
                config.url,
                auth_type=config.auth_type,
                env=config.env,
            )
        else:
            return await self.connect_stdio_server(
                config.name,
                config.command,
                config.args,
                env=config.env,
                cwd=config.cwd,
            )

    async def list_tools(self) -> List[Dict[str, Any]]:
        """List all available tools from all connected servers (any transport)."""
        all_tools = []

        for name, session in self.sessions.items():
            try:
                tools = await session.list_tools()
                self.tool_cache[name] = tools.tools
                for tool in tools.tools:
                    all_tools.append(
                        {
                            "name": tool.name,
                            "description": tool.description or "",
                            "inputSchema": tool.inputSchema or {},
                            "server": name,
                        }
                    )
            except Exception as e:
                logger.error(f"Failed to list tools from server '{name}': {e}")

        return all_tools

    async def execute_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """Execute a tool by name across all connected servers (any transport)."""
        for name, session in self.sessions.items():
            try:
                # Prefer the cached tool list to avoid a round trip per call;
                # fall back to a fresh list_tools() if the cache is empty.
                cached = self.tool_cache.get(name)
                if cached is None:
                    cached = (await session.list_tools()).tools
                    self.tool_cache[name] = cached

                if any(t.name == tool_name for t in cached):
                    async with self._locks.setdefault(name, asyncio.Lock()):
                        logger.info(f"Executing tool '{tool_name}' on server '{name}'")
                        result = await session.call_tool(tool_name, arguments)
                        return result.content[0].text if result.content else ""
            except Exception as e:
                logger.error(f"Failed to execute tool on server '{name}': {e}")

        raise ValueError(f"Tool '{tool_name}' not found on any connected server")

    async def close_all(self):
        """Close all MCP server connections (stdio and HTTP alike).

        Closed in reverse of connection order (LIFO). Each connection's
        AsyncExitStack opens an anyio cancel scope in the current task;
        those scopes nest like a stack, so the most-recently-opened one
        (e.g. 'demo', connected after 'gmail') sits on top and must be
        exited first. Closing in insertion order instead tries to exit the
        outer 'gmail' scope while 'demo's inner scope is still open, which
        anyio rejects with "Attempted to exit a cancel scope that isn't
        the current task's current cancel scope" - this was happening on
        every single run, not intermittently, because 'gmail' is always
        connected first.
        """
        for name, stack in reversed(list(self._stacks.items())):
            try:
                await stack.aclose()
                logger.info(f"Closed connection to server '{name}'")
            except Exception as e:
                logger.error(f"Error closing connection to '{name}': {e}")

        self.sessions.clear()
        self.tool_cache.clear()
        self._locks.clear()
        self._access_tokens.clear()
        self._stacks.clear()

    def get_connected_servers(self) -> List[str]:
        """Get list of connected server names."""
        return list(self.sessions.keys())
