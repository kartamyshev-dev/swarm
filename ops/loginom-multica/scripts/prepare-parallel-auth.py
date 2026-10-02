#!/usr/bin/env python3
"""Move one OAuth identity while idle; a path-only journal makes retries resumable."""
import argparse
import fcntl
import json
import os
from pathlib import Path
from api import API
from common import read_private, write_private
from concurrency import ensure_idle
from shared_auth import MARKER, private_path, shared_directory


def migrate(api, deployment, operator_path, cards, *, source):
    source = Path(source)
    root = private_path(source.parent, directory=True)
    if source.name != 'auth.json' or source != root / 'auth.json':
        raise RuntimeError('ORIGINAL_AUTH_JSON_REQUIRED')
    directory = root / 'provider-auth'
    target = directory / 'auth.json'
    journal_path = root / 'parallel-auth-migration.json'
    migration_lock = os.open(root / '.parallel-auth-migration.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    token_lock = None
    try:
        try:
            fcntl.flock(migration_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('AUTH_MIGRATION_BUSY') from None
        ensure_idle(api, deployment)
        operator = read_private(operator_path)
        if operator['workspace_id'] != deployment['workspace_id']:
            raise RuntimeError('WORKSPACE_MISMATCH')
        paths = [private_path(operator_path)]
        if Path(cards).exists():
            private_path(cards, directory=True)
            for card in Path(cards).iterdir():
                private_path(card, directory=True)
                for role in ['worker', 'reviewer']:
                    path = card / (role + '.json')
                    if path.exists():
                        data = read_private(path)
                        if data.get('operator_file') != str(operator_path):
                            raise RuntimeError('FOREIGN_ROLE_CONFIG')
                        paths.append(private_path(path))
        for path in paths:
            if read_private(path).get('provider_auth_file') not in [str(source), str(target)]:
                raise RuntimeError('AUTH_REFERENCE_MISMATCH')
        expected = {'format': 'loginom-auth-migration-v1', 'source': str(source), 'target': str(target)}
        if journal_path.exists():
            journal = read_private(journal_path)
            if any(journal.get(key) != value for key, value in expected.items()):
                raise RuntimeError('AUTH_MIGRATION_JOURNAL_MISMATCH')
        else:
            if directory.exists():
                raise RuntimeError('NEW_AUTH_DIRECTORY_REQUIRED')
            private_path(source)
            journal = {**expected, 'state': 'prepared'}
            write_private(journal_path, journal)
        if not directory.exists():
            directory.mkdir(mode=0o700)
        private_path(directory, directory=True)
        if source.exists() == target.exists():
            raise RuntimeError('AUTH_MIGRATION_SOURCE_AMBIGUOUS')
        old_lock, new_lock = Path(str(source) + '.lock'), Path(str(target) + '.lock')
        if old_lock.exists() and new_lock.exists():
            raise RuntimeError('AUTH_MIGRATION_LOCK_AMBIGUOUS')
        lock_path = new_lock if new_lock.exists() else old_lock
        token_lock = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        private_path(lock_path)
        try:
            fcntl.flock(token_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('AUTH_BUSY') from None
        ensure_idle(api, deployment)
        if source.exists():
            private_path(source)
            os.rename(source, target)
        if lock_path == old_lock:
            os.rename(old_lock, new_lock)
        if journal.get('state') != 'complete':
            write_private(directory / 'marker.json', MARKER)
        shared_directory(target)
        for path in paths:
            data = read_private(path)
            if data['provider_auth_file'] == str(source):
                write_private(path, {**data, 'provider_auth_file': str(target)})
        if source.exists() or any(read_private(path)['provider_auth_file'] != str(target) for path in paths):
            raise RuntimeError('AUTH_MIGRATION_READBACK_FAILED')
        write_private(journal_path, {**expected, 'state': 'complete', 'configs': len(paths)})
        return {'provider_auth_file': str(target), 'configs': len(paths), 'state': 'complete'}
    finally:
        if token_lock is not None:
            os.close(token_lock)
        os.close(migration_lock)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--deployment', required=True, type=Path)
    parser.add_argument('--operator', type=Path, default=Path.home() / '.config/loginom-multica/operator.json')
    parser.add_argument('--cards', type=Path, default=Path.home() / '.config/loginom-multica/cards')
    parser.add_argument('--source', type=Path, default=Path.home() / '.config/loginom-multica/auth.json')
    parser.add_argument('--confirm-idle', action='store_true', required=True,
                        help='No new cards, mentions, schedules or runs during this maintenance window')
    args = parser.parse_args()
    deployment = json.loads(args.deployment.read_text())
    if deployment['workspace_id'] != os.environ['MULTICA_WORKSPACE_ID']:
        raise RuntimeError('WORKSPACE_MISMATCH')
    print(json.dumps(migrate(API(), deployment, args.operator, args.cards, source=args.source)))


if __name__ == '__main__':
    try:
        main()
    except RuntimeError as error:
        raise SystemExit(str(error))
