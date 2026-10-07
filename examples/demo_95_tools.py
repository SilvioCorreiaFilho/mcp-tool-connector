"""Demo with a 95-tool catalog, the size this package exists to handle.

Runs offline: the tools are local functions. What it measures is the context cost
of the catalog and the behavior under failure.

    python examples/demo_95_tools.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from mcp_tool_connector import IdempotencyCache, IsolatedCaller, ToolCatalog, ToolSpec  # noqa: E402

GROUPS = {
    'crm': ('list_contacts', 'get_contact', 'create_contact', 'update_contact', 'delete_contact',
            'list_deals', 'get_deal', 'create_deal', 'update_deal', 'move_deal_stage',
            'list_pipelines', 'get_pipeline', 'list_activities', 'log_activity', 'list_owners',
            'list_tags', 'add_tag', 'merge_contacts'),
    'ads': ('list_campaigns', 'get_campaign', 'create_campaign', 'update_campaign', 'pause_campaign',
            'resume_campaign', 'list_adsets', 'get_adset', 'create_adset', 'update_adset',
            'list_ads', 'get_ad', 'create_ad', 'update_ad', 'list_creatives', 'get_creative',
            'upload_creative', 'list_audiences', 'get_audience', 'create_audience'),
    'analytics': ('query_metrics', 'get_timeseries', 'get_breakdown', 'list_events', 'get_funnel',
                  'get_attribution', 'export_report', 'list_segments', 'get_segment',
                  'create_segment', 'list_dashboards', 'get_dashboard'),
    'messaging': ('send_message', 'list_conversations', 'get_conversation', 'assign_conversation',
                  'close_conversation', 'list_templates', 'get_template', 'create_template',
                  'send_template', 'list_channels', 'get_channel', 'list_agents',
                  'list_messages', 'get_message', 'list_optouts'),
    'billing': ('list_invoices', 'get_invoice', 'create_invoice', 'void_invoice', 'list_payments',
                'get_payment', 'refund_payment', 'list_plans', 'get_plan', 'list_subscriptions',
                'get_subscription', 'cancel_subscription'),
    'platform': ('list_users', 'get_user', 'create_user', 'update_user', 'list_roles', 'get_role',
                 'list_webhooks', 'create_webhook', 'delete_webhook', 'list_audit_log',
                 'get_audit_entry', 'list_api_keys', 'rotate_api_key', 'get_health',
                 'get_usage', 'list_limits', 'get_limit', 'update_limit'),
}
READ_PREFIXES = ('list_', 'get_', 'query_', 'export_')


def build_demo_catalog() -> ToolCatalog:
    specs = []
    for group, names in GROUPS.items():
        for name in names:
            is_read = name.startswith(READ_PREFIXES)
            specs.append(ToolSpec(
                name=f'{group}_{name}',
                description=(f'{name.replace("_", " ").capitalize()} in the {group} module. '
                             f'Returns {group} records honoring the given filters and the '
                             f'service default pagination.'),
                input_schema={
                    'type': 'object',
                    'properties': {
                        'id': {'type': 'string', 'description': f'Record identifier in {group}'},
                        'limit': {'type': 'integer', 'description': 'Max items (1-200)'},
                        'filters': {'type': 'object', 'description': 'Module-specific filters'},
                    },
                    'required': [] if is_read else ['id'],
                },
                tags=(group, 'read' if is_read else 'write'),
            ))
    return ToolCatalog(specs)


def main() -> None:
    cat = build_demo_catalog()
    line = '=' * 74
    print(f'{line}\nCATALOG: {cat.summary()}\n{line}')
    print('\ncontext cost per module (estimate, ~4 chars/token):')
    for tag, tokens in cat.cost_by_tag().items():
        print(f'  {tag:12} {tokens:>7} tokens')

    print(f'\n{line}\nPER-ROLE SLICE: same catalog, whole vs. sliced\n{line}')
    whole = cat.context_tokens()
    sliced = cat.select_for_role(tags=['ads', 'analytics'], budget_tokens=4000,
                                 always_include=['platform_get_health'])
    print(f'  full catalog : {len(cat):>3} tools  ~{whole:>7} schema tokens')
    print(f'  ads role     : {len(sliced):>3} tools  ~{sliced.context_tokens():>7} schema tokens')
    print(f'  saved        : {100 * (1 - sliced.context_tokens() / whole):.0f}% of schema context')

    print(f'\n{line}\nFAILURE ISOLATION: one broken tool does not take the agent down\n{line}')
    attempts = {'broken': 0, 'flaky': 0}

    def good(**kw):
        return {'records': [{'id': 1}, {'id': 2}]}

    def broken(**kw):
        attempts['broken'] += 1
        raise TimeoutError('upstream did not answer in 30s')

    def flaky(**kw):
        if attempts['flaky'] < 1:
            attempts['flaky'] += 1
            raise ConnectionError('connection refused')
        return {'ok': True}

    tools = {'ads_list_campaigns': good, 'ads_get_campaign': broken, 'ads_get_ad': flaky}
    with IsolatedCaller(tools, read_only={'ads_list_campaigns'}, cache=IdempotencyCache(),
                        failure_threshold=3, cooldown_seconds=60) as caller:
        r = caller.call('ads_list_campaigns', {'limit': 10})
        print(f'  good call      -> ok={r.ok} ({r.duration_ms}ms)')
        r = caller.call('ads_list_campaigns', {'limit': 10})
        print(f'  same call      -> cached={r.cached} (read dedup)')
        r = caller.call('ads_get_campaign', {'id': 'x'})
        print(f'  broken tool    -> ok={r.ok}; agent sees: {r.as_agent_message()[:70]}...')
        for _ in range(3):
            caller.call('ads_get_campaign', {'id': 'x'})
        r = caller.call('ads_get_campaign', {'id': 'x'})
        print(f'  after 3 fails  -> blocked={"circuit open" in r.error}; '
              f'real attempts={attempts["broken"]} (rest cut by the breaker)')
        r1, r2 = caller.call('ads_get_ad', {'id': 'y'}), caller.call('ads_get_ad', {'id': 'y'})
        print(f'  transient fail -> first ok={r1.ok}, retry ok={r2.ok} (no circuit on first failure)')
        r = caller.call('tool_that_does_not_exist')
        print(f'  wrong name     -> ok={r.ok} error={r.error[:40]!r}')
        print(f'\n  summary: {caller.summary()}')


if __name__ == '__main__':
    main()
