import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'examples'))

from demo_95_tools import build_demo_catalog  # noqa: E402

from mcp_tool_connector import ToolCatalog, ToolSpec, build_catalog  # noqa: E402


def test_demo_catalog_has_95_tools():
    assert len(build_demo_catalog()) == 95


def test_from_mcp_response_skips_malformed_entries():
    cat = ToolCatalog.from_mcp_response({'tools': [
        {'name': 'a', 'description': 'A', 'inputSchema': {'type': 'object'}},
        {'description': 'no name'},
        'garbage',
        {'name': 'b'},
    ]})
    assert cat.names() == ['a', 'b']
    assert cat.get('a').input_schema == {'type': 'object'}


def test_from_mcp_response_handles_empty():
    assert len(ToolCatalog.from_mcp_response(None)) == 0
    assert len(ToolCatalog.from_mcp_response({})) == 0


def test_select_for_role_by_tag_and_name():
    cat = build_demo_catalog()
    s = cat.select_for_role(tags=['billing'], names=['crm_get_contact'])
    assert all('billing' in t.tags or t.name == 'crm_get_contact' for t in s)
    assert 'crm_get_contact' in s and len(s) == 13


def test_select_for_role_no_match_returns_empty_not_full():
    assert len(build_demo_catalog().select_for_role(tags=['nope'])) == 0


def test_budget_is_respected_and_pins_kept():
    cat = build_demo_catalog()
    s = cat.select_for_role(tags=['ads', 'analytics'], budget_tokens=4000,
                            always_include=['platform_get_health'])
    assert 'platform_get_health' in s
    assert s.context_tokens() <= 4000
    assert s.context_tokens() < cat.context_tokens() / 2


def test_cost_by_tag_sorted_desc():
    costs = list(build_demo_catalog().cost_by_tag().values())
    assert costs == sorted(costs, reverse=True)


def test_round_trip_to_mcp_shape():
    cat = build_demo_catalog()
    again = ToolCatalog.from_mcp_response({'tools': cat.to_mcp_tools()})
    assert again.names() == cat.names()


def test_build_catalog_shortcut():
    cat = build_catalog({'a': 'does A', 'b': {'type': 'object', 'properties': {'x': {}}}},
                        tags={'a': ['read']})
    assert cat.get('a').description == 'does A' and cat.get('a').tags == ('read',)
    assert cat.get('b').input_schema['properties'] == {'x': {}}


def test_schema_json_is_compact_and_unicode():
    s = ToolSpec('t', 'ação', {'type': 'object'})
    assert json.loads(s.schema_json)['description'] == 'ação'
    assert ' ' not in s.schema_json.replace('ação', '')
