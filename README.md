# mcp-tool-connector

Run **large MCP tool catalogs** in production without letting them eat the context
window or take the agent down.

[![tests](https://github.com/SilvioCorreiaFilho/mcp-tool-connector/actions/workflows/tests.yml/badge.svg)](https://github.com/SilvioCorreiaFilho/mcp-tool-connector/actions/workflows/tests.yml)

An agent wired to ~100 tools rarely fails for lack of a tool. It fails because:

1. **The whole catalog schema rides along on every request.** Tens of thousands of
   tokens of tool definitions push history out of the window and make tool selection worse.
2. **One bad tool breaks the turn.** A timeout or exception in a single tool surfaces
   as a crash, or as an error message the model misreads and retries forever.
3. **Blind retries duplicate side effects.** The model calls `create_invoice` twice
   because the first answer looked odd.

This package handles all three. Standard library only, no dependencies, on purpose.

| Problem | Component | What it does |
|---|---|---|
| Context bloat | `ToolCatalog.select_for_role()` | Per-role slice by tag/name under a token budget; drops the most expensive schemas first; **no match returns an empty slice, never the full catalog** |
| Failure blast radius | `IsolatedCaller` | Tool exceptions become results, enforced timeout, per-tool circuit breaker with cooldown and half-open retry |
| Duplicate effects | `IdempotencyCache` | TTL dedup keyed by tool + canonical args, **opt-in for read-only tools only**; writes are never cached |
| Talking to servers | `McpHttpClient` | Minimal MCP Streamable HTTP client: `initialize`, session id, bearer auth, JSON and SSE bodies, `tools/list`, `tools/call` |

## Real run

`python examples/demo_95_tools.py` (offline, synthetic 95-tool catalog):

```console
CATALOG: 95 tools, 40667 chars (~10119 schema tokens)

PER-ROLE SLICE: same catalog, whole vs. sliced
  full catalog :  95 tools  ~  10119 schema tokens
  ads role     :  33 tools  ~   3484 schema tokens
  saved        : 66% of schema context

FAILURE ISOLATION: one broken tool does not take the agent down
  good call      -> ok=True (0ms)
  same call      -> cached=True (read dedup)
  broken tool    -> ok=False; agent sees: ads_get_campaign: FAILED: TimeoutError: upstream did not answer in 30s...
  after 3 fails  -> blocked=True; real attempts=3 (rest cut by the breaker)
  transient fail -> first ok=False, retry ok=True (no circuit on first failure)
  wrong name     -> ok=False error='unknown tool. Available: ads_get_ad, ads'
```

Against a live server ([Scrapling](https://github.com/D4Vinci/Scrapling) MCP over
Streamable HTTP, 13 tools), the full catalog is ~14.3k schema tokens; a research role
sliced to `make_request` + `fetch` is ~3.1k (78% less). The second identical read was
served from cache, and a failing `fetch` came back as a readable `ToolCall` instead of
an exception.

Token counts are a ~4 chars/token **estimate**, meant to compare slices against each
other, not to predict a bill.

## Usage against a real MCP server

```python
import os
from mcp_tool_connector import McpHttpClient, IsolatedCaller, IdempotencyCache

client = McpHttpClient('http://mcp.internal:8765/mcp', token=os.environ['MCP_TOKEN'])
catalog = client.catalog()                                  # tools/list
print(catalog.summary(), catalog.cost_by_tag())

# The research role only gets read tools, under a 4k-token schema budget
research = catalog.select_for_role(names=['make_request', 'fetch'], budget_tokens=4000)

with IsolatedCaller(client.callables(research),
                    read_only=set(research.names()),
                    cache=IdempotencyCache(ttl_seconds=300),
                    timeout_seconds=60, failure_threshold=3, cooldown_seconds=120) as caller:
    r = caller.call('make_request', url='https://example.com')
    print(r.as_agent_message())       # what goes back to the model, success or failure
```

`IsolatedCaller` accepts any `{name: callable}`, so the same isolation applies to
local Python tools, HTTP APIs, or MCP servers.

## Design decisions

- **Errors are data.** `ToolCall.as_agent_message()` tells the model explicitly not to
  repeat a failed call, because models otherwise assume they got the arguments wrong.
- **Empty beats everything.** A role filter that matches nothing returns nothing. Giving
  the full catalog by accident is the exact failure this exists to prevent.
- **Timeouts are honest.** Python cannot kill a thread; on timeout the call returns a
  failure immediately, counts toward the breaker, and the worker finishes in the
  background. The pool is bounded for that reason.
- **Cache is opt-in per tool.** Deduplicating a write is a correctness bug, so only
  tools declared read-only are cached.

## Install and test

```bash
pip install -e ".[test]"
pytest                      # 27 tests: catalog, caller, and transport vs. an in-process fake MCP server
python examples/demo_95_tools.py
```

Python 3.10+. MIT licensed.

## Related

- [agent-deploy-gate](https://github.com/SilvioCorreiaFilho/agent-deploy-gate): deterministic
  deploy gate for agent workloads (snapshot, invariants, verify, rollback, audit).
