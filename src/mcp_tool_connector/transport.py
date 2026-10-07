"""Minimal MCP client over Streamable HTTP, standard library only.

Enough of the protocol to discover a server's tools and call them:
`initialize` -> `notifications/initialized` -> `tools/list` / `tools/call`,
with session id tracking, bearer auth and both JSON and SSE response bodies.

    client = McpHttpClient('http://100.x.y.z:8765/mcp', token=os.environ['MCP_TOKEN'])
    catalog = client.catalog()
    caller = IsolatedCaller(client.callables(catalog.select_for_role(tags=['read'])))
"""
from __future__ import annotations

import json
import urllib.request
from typing import Any, Callable, Mapping

from .catalog import ToolCatalog

PROTOCOL_VERSION = '2025-03-26'


class McpError(RuntimeError):
    """A JSON-RPC error, or a tool result flagged with isError."""


class McpHttpClient:
    def __init__(self, url: str, token: str | None = None, timeout: float = 120.0,
                 client_name: str = 'mcp-tool-connector'):
        self.url = url
        self.token = token
        self.timeout = timeout
        self.client_name = client_name
        self.session_id: str | None = None
        self._next_id = 0
        self._initialized = False

    # ------------------------------------------------------------------ wire
    def _post(self, payload: Mapping[str, Any]) -> dict | None:
        headers = {'Content-Type': 'application/json',
                   'Accept': 'application/json, text/event-stream'}
        if self.token:
            headers['Authorization'] = f'Bearer {self.token}'
        if self.session_id:
            headers['Mcp-Session-Id'] = self.session_id
        req = urllib.request.Request(self.url, json.dumps(payload).encode(), headers, method='POST')
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            self.session_id = resp.headers.get('Mcp-Session-Id') or self.session_id
            body = resp.read().decode('utf-8', 'replace')
            content_type = resp.headers.get('Content-Type', '')
        if not body.strip():
            return None
        if 'event-stream' in content_type:
            events = [json.loads(line[5:]) for line in body.splitlines() if line.startswith('data:')]
            return events[-1] if events else None
        return json.loads(body)

    def _rpc(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        self._next_id += 1
        reply = self._post({'jsonrpc': '2.0', 'id': self._next_id,
                            'method': method, 'params': dict(params or {})})
        if reply is None:
            raise McpError(f'{method}: empty response')
        if 'error' in reply:
            raise McpError(f"{method}: {reply['error']}")
        return reply.get('result')

    # ------------------------------------------------------------------ protocol
    def initialize(self) -> dict:
        result = self._rpc('initialize', {
            'protocolVersion': PROTOCOL_VERSION, 'capabilities': {},
            'clientInfo': {'name': self.client_name, 'version': '1'}})
        self._post({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        self._initialized = True
        return result or {}

    def _ensure(self) -> None:
        if not self._initialized:
            self.initialize()

    def list_tools(self) -> dict:
        self._ensure()
        return self._rpc('tools/list') or {}

    def catalog(self) -> ToolCatalog:
        return ToolCatalog.from_mcp_response(self.list_tools())

    def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> str:
        """Call a tool and return its text content. Raises McpError on isError."""
        self._ensure()
        result = self._rpc('tools/call', {'name': name, 'arguments': dict(arguments or {})}) or {}
        parts = [c.get('text', '') if c.get('type') == 'text' else f"<{c.get('type')}>"
                 for c in result.get('content', [])]
        text = '\n'.join(parts)
        if result.get('isError'):
            raise McpError(text or f'{name}: tool reported an error')
        return text

    def callables(self, catalog: ToolCatalog) -> dict[str, Callable[..., str]]:
        """{name: fn(**kwargs)} for every tool in the catalog, ready for IsolatedCaller."""
        def make(name: str) -> Callable[..., str]:
            return lambda **kwargs: self.call_tool(name, kwargs)
        return {spec.name: make(spec.name) for spec in catalog}
