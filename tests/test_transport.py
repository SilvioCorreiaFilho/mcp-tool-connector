"""Transport tests against a tiny in-process fake MCP server (no network beyond localhost)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from mcp_tool_connector import IsolatedCaller, McpError, McpHttpClient

TOKEN = 'test-token'


class FakeMcp(BaseHTTPRequestHandler):
    seen_sessions: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        if self.headers.get('Authorization') != f'Bearer {TOKEN}':
            self.send_response(401); self.end_headers(); return
        msg = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        FakeMcp.seen_sessions.append(self.headers.get('Mcp-Session-Id'))
        method = msg.get('method')
        if 'id' not in msg:                                # notification
            self.send_response(202); self.end_headers(); return
        if method == 'initialize':
            result = {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}},
                      'serverInfo': {'name': 'fake', 'version': '0'}}
        elif method == 'tools/list':
            result = {'tools': [
                {'name': 'echo', 'description': 'echo back', 'inputSchema': {'type': 'object'}},
                {'name': 'fail', 'description': 'always errors', 'inputSchema': {'type': 'object'}},
            ]}
        elif method == 'tools/call':
            name, args = msg['params']['name'], msg['params']['arguments']
            if name == 'echo':
                result = {'content': [{'type': 'text', 'text': json.dumps(args)}]}
            elif name == 'fail':
                result = {'content': [{'type': 'text', 'text': 'upstream exploded'}], 'isError': True}
            else:
                body = {'jsonrpc': '2.0', 'id': msg['id'], 'error': {'code': -32602, 'message': 'unknown'}}
                return self._send(body, sse=False)
        else:
            result = {}
        # answer tools/call as SSE, everything else as plain JSON: exercises both parsers
        self._send({'jsonrpc': '2.0', 'id': msg['id'], 'result': result}, sse=(method == 'tools/call'))

    def _send(self, body, sse):
        self.send_response(200)
        self.send_header('Mcp-Session-Id', 'sess-1')
        if sse:
            data = f'event: message\ndata: {json.dumps(body)}\n\n'.encode()
            self.send_header('Content-Type', 'text/event-stream')
        else:
            data = json.dumps(body).encode()
            self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def server_url():
    srv = HTTPServer(('127.0.0.1', 0), FakeMcp)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield f'http://127.0.0.1:{srv.server_port}/mcp'
    srv.shutdown()


def test_discovery_and_call_with_session(server_url):
    c = McpHttpClient(server_url, token=TOKEN)
    cat = c.catalog()
    assert cat.names() == ['echo', 'fail']
    assert json.loads(c.call_tool('echo', {'x': 1})) == {'x': 1}
    assert c.session_id == 'sess-1'
    assert 'sess-1' in FakeMcp.seen_sessions          # session header sent after initialize


def test_is_error_raises(server_url):
    with pytest.raises(McpError, match='upstream exploded'):
        McpHttpClient(server_url, token=TOKEN).call_tool('fail')


def test_jsonrpc_error_raises(server_url):
    with pytest.raises(McpError, match='unknown'):
        McpHttpClient(server_url, token=TOKEN).call_tool('nope')


def test_bad_token_is_http_error(server_url):
    import urllib.error
    with pytest.raises(urllib.error.HTTPError):
        McpHttpClient(server_url, token='wrong').list_tools()


def test_end_to_end_with_isolated_caller(server_url):
    c = McpHttpClient(server_url, token=TOKEN)
    with IsolatedCaller(c.callables(c.catalog()), failure_threshold=2) as caller:
        ok = caller.call('echo', a='b')
        bad = caller.call('fail')
        caller.call('fail')
        blocked = caller.call('fail')
    assert ok.ok and json.loads(ok.result) == {'a': 'b'}
    assert not bad.ok and 'upstream exploded' in bad.error
    assert 'circuit open' in blocked.error
