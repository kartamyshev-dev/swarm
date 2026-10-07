#!/usr/bin/env python3
"""Load the approved instruction templates through the native API, with read-back."""
import argparse
import copy
import json
from pathlib import Path
from api import API
from common import read_private
from memory import mcp_server


def configure(api, config, root, ref=None, memory_config=None, *, instructions_only=False):
    if instructions_only and memory_config is not None:
        raise RuntimeError('INSTRUCTIONS_ONLY_CONFIG_CONFLICT')
    if not instructions_only and not ref:
        raise RuntimeError('PROJECT_REF_REQUIRED')
    resources_path = 'projects/' + config['project_id'] + '/resources' if instructions_only and config.get('project_id') else None
    resources = copy.deepcopy(api.request(resources_path)) if resources_path else None
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
        previous = copy.deepcopy(api.request('agents/' + id)) if instructions_only or memory_config is not None else None
        data = {'instructions': template(role)}
        if not instructions_only:
            limit = config.get('max_concurrent_tasks', {}).get(role, 1)
            if type(limit) is not int or not 1 <= limit <= 50:
                raise RuntimeError('CONCURRENCY_LIMIT_INVALID')
            data['max_concurrent_tasks'] = limit
        if memory_config is not None:
            if previous.get('mcp_config_redacted'):
                raise RuntimeError('MCP_CONFIG_READ_ACCESS_REQUIRED')
            mcp = previous.get('mcp_config') or {}
            data['mcp_config'] = {**mcp, 'mcpServers': {
                **mcp.get('mcpServers', {}), 'openviking': mcp_server(memory_config)}}
        api.request('agents/' + id, data)
        persisted = api.request('agents/' + id)
        if any(persisted.get(key) != value for key, value in data.items()):
            raise RuntimeError('AGENT_READBACK_MISMATCH')
        if instructions_only and settings(previous) != settings(persisted):
            raise RuntimeError('AGENT_SETTINGS_CHANGED')
    previous = copy.deepcopy(api.request('squads/' + config['squad_id'])) if instructions_only else None
    data = {'instructions': template('squad')}
    if not instructions_only:
        data['leader_id'] = config['agents']['generator']
    api.request('squads/' + config['squad_id'], data)
    persisted = api.request('squads/' + config['squad_id'])
    if any(persisted.get(key) != value for key, value in data.items()):
        raise RuntimeError('SQUAD_READBACK_MISMATCH')
    if instructions_only:
        if settings(previous) != settings(persisted):
            raise RuntimeError('SQUAD_SETTINGS_CHANGED')
        if resources_path and api.request(resources_path) != resources:
            raise RuntimeError('PROJECT_SETTINGS_CHANGED')
        return
    path = 'projects/' + config['project_id'] + '/resources/' + config['resource_id']
    api.request(path, {'resource_ref': {'url': config['repository'], 'ref': ref}})
    resources = api.request('projects/' + config['project_id'] + '/resources')['resources']
    persisted = next(r for r in resources if r['id'] == config['resource_id'])
    if persisted.get('resource_ref') != {'url': config['repository'], 'ref': ref}:
        raise RuntimeError('PROJECT_READBACK_MISMATCH')


def settings(response):
    # Liveness and update times can move independently of configuration writes.
    return {key: value for key, value in response.items()
            if key not in {'instructions', 'updated_at', 'status', 'runtime_availability', 'member_preview'}}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--deployment', required=True, type=Path)
    parser.add_argument('--pilot-receipt', required=True, type=Path)
    parser.add_argument('--ref')
    parser.add_argument('--instructions-only', action='store_true')
    parser.add_argument('--memory-config', type=Path)
    args = parser.parse_args()
    deployment = json.loads(args.deployment.read_text())
    receipt = json.loads(args.pilot_receipt.read_text())
    if receipt.get('status') != 'PASS' or not receipt.get('source_sha') or not receipt.get('worker_comment') or not receipt.get('reviewer_comment'):
        raise SystemExit('SUCCESSFUL_PILOT_REQUIRED')
    memory_config = read_private(args.memory_config) if args.memory_config else None
    configure(API(), deployment, Path(__file__).resolve().parents[1], args.ref, memory_config,
              instructions_only=args.instructions_only)
    print('configuration_readback_verified')
