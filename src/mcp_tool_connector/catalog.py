"""Tool catalog: discovery, context cost, and per-role slicing.

The core idea of this module: **a large catalog is never handed to an agent whole.**
Every tool schema is text that lands in the model context on every request. With ~100
tools that is tens of thousands of tokens per call, pushing history out of the window
and degrading tool selection. The fix is to give each role only the slice it uses.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Mapping, Sequence


@dataclass(frozen=True)
class ToolSpec:
    """One tool, as an MCP server describes it."""

    name: str
    description: str = ''
    input_schema: dict = field(default_factory=dict)
    tags: tuple[str, ...] = ()

    @property
    def schema_json(self) -> str:
        """The representation that actually goes into the model context."""
        return json.dumps(
            {'name': self.name, 'description': self.description,
             'input_schema': self.input_schema},
            ensure_ascii=False, separators=(',', ':'),
        )

    @property
    def context_chars(self) -> int:
        return len(self.schema_json)

    @property
    def context_tokens(self) -> int:
        """Estimate at ~4 characters per token.

        This is a HEURISTIC, not real tokenization. It is meant to compare slices
        against each other ("the full catalog costs 12x the slice"), not to predict
        an invoice.
        """
        return self.context_chars // 4


class ToolCatalog:
    """A collection of ToolSpec with lookup by name, tag and token budget."""

    def __init__(self, specs: Iterable[ToolSpec] = ()):
        self._by_name: dict[str, ToolSpec] = {s.name: s for s in specs}

    # ------------------------------------------------------------------ build
    @classmethod
    def from_mcp_response(cls, response: Mapping[str, Any] | None) -> 'ToolCatalog':
        """Build from the result of an MCP `tools/list` call.

        Malformed entries (not a dict, missing name) are skipped instead of failing
        the whole catalog: one bad tool definition should not hide the other 94.
        """
        raw = (response or {}).get('tools') or []
        specs = []
        for t in raw:
            if not isinstance(t, dict) or not t.get('name'):
                continue
            specs.append(ToolSpec(
                name=t['name'],
                description=str(t.get('description') or ''),
                input_schema=t.get('inputSchema') or t.get('input_schema') or {},
                tags=tuple(t.get('tags') or ()),
            ))
        return cls(specs)

    def add(self, spec: ToolSpec) -> None:
        self._by_name[spec.name] = spec

    # ------------------------------------------------------------------ lookup
    def __len__(self) -> int:
        return len(self._by_name)

    def __contains__(self, name: object) -> bool:
        return name in self._by_name

    def __iter__(self) -> Iterator[ToolSpec]:
        return iter(self._by_name.values())

    def names(self) -> list[str]:
        return sorted(self._by_name)

    def get(self, name: str) -> ToolSpec | None:
        return self._by_name.get(name)

    def by_tag(self, tag: str) -> list[ToolSpec]:
        return [s for s in self if tag in s.tags]

    # ------------------------------------------------------------------ cost
    def context_chars(self) -> int:
        return sum(s.context_chars for s in self)

    def context_tokens(self) -> int:
        return sum(s.context_tokens for s in self)

    def cost_by_tag(self) -> dict[str, int]:
        """Where the context budget is going, largest first."""
        spent: dict[str, int] = {}
        for s in self:
            for tag in (s.tags or ('<untagged>',)):
                spent[tag] = spent.get(tag, 0) + s.context_tokens
        return dict(sorted(spent.items(), key=lambda kv: -kv[1]))

    # ------------------------------------------------------------------ slicing
    def select_for_role(
        self,
        tags: Sequence[str] = (),
        names: Sequence[str] = (),
        budget_tokens: int | None = None,
        always_include: Sequence[str] = (),
    ) -> 'ToolCatalog':
        """The slice one role receives.

        Selects by tag or by name, always keeps `always_include`, and enforces a
        token budget by dropping the most expensive schemas first (they cost the
        most context per unit of usefulness).

        If nothing matches, an EMPTY catalog is returned, never the full one:
        handing over everything by accident is the failure mode this method exists
        to prevent.
        """
        chosen: dict[str, ToolSpec] = {}
        for s in self:
            if (s.name in always_include or s.name in names
                    or (tags and any(t in s.tags for t in tags))):
                chosen[s.name] = s

        if budget_tokens is not None:
            pinned = {n: self._by_name[n] for n in always_include if n in self._by_name}
            spent = sum(s.context_tokens for s in pinned.values())
            rest = sorted((s for n, s in chosen.items() if n not in pinned),
                          key=lambda s: s.context_tokens)
            chosen = dict(pinned)
            for s in rest:
                if spent + s.context_tokens > budget_tokens:
                    break
                chosen[s.name] = s
                spent += s.context_tokens

        return ToolCatalog(chosen.values())

    def to_mcp_tools(self) -> list[dict]:
        """Back to the MCP wire shape, e.g. to expose a slice through a proxy server."""
        return [{'name': s.name, 'description': s.description, 'inputSchema': s.input_schema}
                for s in self]

    def summary(self) -> str:
        return (f'{len(self)} tools, {self.context_chars()} chars '
                f'(~{self.context_tokens()} schema tokens)')


def build_catalog(tools: Mapping[str, Any],
                  tags: Mapping[str, Sequence[str]] | None = None) -> ToolCatalog:
    """Shortcut: build from a {name: description | json-schema} mapping."""
    tags = tags or {}
    specs = []
    for name, desc in tools.items():
        specs.append(ToolSpec(
            name=name,
            description=desc if isinstance(desc, str) else '',
            input_schema=desc if isinstance(desc, dict) else {'type': 'object', 'properties': {}},
            tags=tuple(tags.get(name, ())),
        ))
    return ToolCatalog(specs)
