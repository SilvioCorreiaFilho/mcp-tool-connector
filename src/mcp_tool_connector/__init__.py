"""mcp-tool-connector: put a large MCP tool catalog into production safely.

An agent with ~100 tools rarely fails for lack of a tool. It fails because
  1. the full catalog schema eats the context window,
  2. one bad tool takes the whole call down,
  3. blind retries duplicate side effects.

This package addresses all three, with the standard library only.
"""
from .caller import IdempotencyCache, IsolatedCaller, ToolCall
from .catalog import ToolCatalog, ToolSpec, build_catalog
from .transport import McpError, McpHttpClient

__version__ = '1.1.0'
__all__ = [
    'ToolSpec', 'ToolCatalog', 'build_catalog',
    'ToolCall', 'IsolatedCaller', 'IdempotencyCache',
    'McpHttpClient', 'McpError',
]
