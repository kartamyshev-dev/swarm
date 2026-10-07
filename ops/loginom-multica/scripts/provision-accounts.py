#!/usr/bin/env python3
"""Persist a pair before UI creation; retries reuse identities and reconcile uncertainty."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import secrets
import subprocess
from uuid import UUID
from common import read_private, write_private


def allocate(issue, operator_path, directory):
    issue = str(UUID(issue))
    operator = read_private(operator_path)
    auth_files = operator.get('provider_auth_files')
    if (not isinstance(auth_files, list) or len(auth_files) != 8 or
            any(not isinstance(path, str) or not Path(path).is_absolute() for path in auth_files) or
            len(set(auth_files)) != 8):
        raise RuntimeError('EIGHT_PROVIDER_AUTH_FILES_REQUIRED')
    directory = Path(directory) / issue
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = os.open(directory / '.accounts.lock', os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX)
    configs = []
    try:
        for role in ['worker', 'reviewer']:
            path = directory / (role + '.json')
            if path.exists():
                config = read_private(path)
                if config['issue_id'] != issue or config['role'] != role or config['agent_id'] != operator['agents'][role]:
                    raise RuntimeError('ACCOUNT_BINDING_MISMATCH')
                if config['workspace_id'] != operator['workspace_id']:
                    raise RuntimeError('WORKSPACE_MISMATCH')
                updated = {**config, 'operator_file': str(operator_path), 'provider_auth_files': auth_files}
                updated.pop('provider_auth_file', None)
                if updated != config:
                    write_private(path, updated)
            else:
                config = {'workspace_id': operator['workspace_id'], 'issue_id': issue, 'agent_id': operator['agents'][role], 'role': role,
                          'marker': 'Multica ' + issue + ' ' + role, 'account_state': 'planned',
                          'loginom': {'url': operator['url'], 'username': 'mc-' + issue.replace('-', '')[:20] + '-' + role[0], 'password': secrets.token_urlsafe(18), 'api_key': operator['api_key']},
                          'model': operator.get('model', 'openai/gpt-6.1-sol'), 'variant': 'low',
                          'operator_file': str(operator_path), 'provider_auth_files': auth_files, 'model_cache_file': operator.get('model_cache_file')}
                write_private(path, config)
            configs.append(path)
    finally:
        os.close(lock)
    return configs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--issue', required=True)
    parser.add_argument('--operator', type=Path, default=Path.home() / '.config/loginom-multica/operator.json')
    parser.add_argument('--directory', type=Path, default=Path.home() / '.config/loginom-multica/cards')
    parser.add_argument('--allocate-only', action='store_true')
    args = parser.parse_args()
    configs = allocate(args.issue, args.operator, args.directory)
    operator = read_private(args.operator)
    if not args.allocate_only:
        lock = os.open(configs[0].parent / '.accounts.lock', os.O_RDWR)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for config in configs:
                result = subprocess.run([operator['node'], Path(__file__).with_name('provision-account.mjs'), '--config', config, '--operator', args.operator], capture_output=True, text=True, timeout=300, pass_fds=(lock,))
                if result.returncode:
                    raise RuntimeError('ACCOUNT_PROVISION_FAILED: ' + config.name)
        finally:
            os.close(lock)
    print(json.dumps({'issue_id': str(UUID(args.issue)), 'configs': [str(path) for path in configs], 'verified': not args.allocate_only}))


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, subprocess.TimeoutExpired) as error:
        print(str(error) if isinstance(error, RuntimeError) else 'ACCOUNT_PROVISION_TIMEOUT', file=__import__('sys').stderr)
        raise SystemExit(1)
