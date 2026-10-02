#!/usr/bin/env python3
"""Load the approved instruction templates through the native API, with read-back."""
import argparse
import json
from pathlib import Path
from api import API
from common import read_private
from memory import mcp_server


def configure(api, config, root, ref, memory_config=None):
    replacements = {'@@OPS@@': config['ops_path'], '@@GEN@@': config['agents']['generator'],
                    '@@WORKER@@': config['agents']['worker'], '@@REVIEWER@@': config['agents']['reviewer']}
    def template(name):
        body = (root / 'instructions' / (name + '.md')).read_text()
        if '@@OWNER@@' in body:
            owner_id = config.get('owner_id') or api.request('me')['id']
            body = body.replace('@@OWNER@@', owner_id)
        for key, value in replacements.items():
            body = body.replace(key, value)
        return body
    for role, id in config['agents'].items():
        limit = config.get('max_concurrent_tasks', {}).get(role, 1)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise RuntimeError('CONCURRENCY_LIMIT_INVALID')
        data = {'instructions': template(role), 'max_concurrent_tasks': limit}
        if memory_config is not None:
            previous = api.request('agents/' + id)
            if previous.get('mcp_config_redacted'):
                raise RuntimeError('MCP_CONFIG_READ_ACCESS_REQUIRED')
            mcp = previous.get('mcp_config') or {}
            data['mcp_config'] = {**mcp, 'mcpServers': {
                **mcp.get('mcpServers', {}), 'openviking': mcp_server(memory_config)}}
        api.request('agents/' + id, data)
        persisted = api.request('agents/' + id)
        if any(persisted.get(key) != value for key, value in data.items()):
            raise RuntimeError('AGENT_READBACK_MISMATCH')
    data = {'leader_id': config['agents']['generator'], 'instructions': template('squad')}
    api.request('squads/' + config['squad_id'], data)
    persisted = api.request('squads/' + config['squad_id'])
    if any(persisted.get(key) != value for key, value in data.items()):
        raise RuntimeError('SQUAD_READBACK_MISMATCH')
    path = 'projects/' + config['project_id'] + '/resources/' + config['resource_id']
    api.request(path, {'resource_ref': {'url': config['repository'], 'ref': ref}})
    resources = api.request('projects/' + config['project_id'] + '/resources')['resources']
    persisted = next(r for r in resources if r['id'] == config['resource_id'])
    if persisted.get('resource_ref') != {'url': config['repository'], 'ref': ref}:
        raise RuntimeError('PROJECT_READBACK_MISMATCH')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--deployment', required=True, type=Path)
    parser.add_argument('--pilot-receipt', required=True, type=Path)
    parser.add_argument('--ref', required=True)
    parser.add_argument('--memory-config', type=Path)
    args = parser.parse_args()
    deployment = json.loads(args.deployment.read_text())
    receipt = json.loads(args.pilot_receipt.read_text())
    if receipt.get('status') != 'PASS' or not receipt.get('source_sha') or not receipt.get('worker_comment') or not receipt.get('reviewer_comment'):
        raise SystemExit('SUCCESSFUL_PILOT_REQUIRED')
    memory_config = read_private(args.memory_config) if args.memory_config else None
    configure(API(), deployment, Path(__file__).resolve().parents[1], args.ref, memory_config)
    print('configuration_readback_verified')
