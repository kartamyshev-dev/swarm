#!/usr/bin/env python3
"""Publish small checked evidence with the native CLI; never set Done or remove data."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
from api import API
from common import checked_path, managed_root, read_private, write_private


def verify_upload(api, issue, comment, files):
    attachments = comment.get('attachments', [])
    if comment.get('issue_id') != issue or len(attachments) != len(files):
        raise RuntimeError('ATTACHMENT_BINDING_MISMATCH')
    for name, expected in files.items():
        matches = [a for a in attachments if a['filename'] == name]
        if len(matches) != 1 or matches[0].get('comment_id') != comment['id']:
            raise RuntimeError('ATTACHMENT_BINDING_MISMATCH')
        body = api.request('attachments/' + matches[0]['id'] + '/download', binary=True)
        if hashlib.sha256(body).hexdigest() != expected:
            raise RuntimeError('ATTACHMENT_CONTENT_MISMATCH')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--worktree', required=True, type=Path)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--attempt', required=True, type=Path)
    parser.add_argument('--content-file', required=True, type=Path)
    parser.add_argument('--parent')
    args = parser.parse_args()
    config = read_private(args.config)
    root, owner = managed_root(args.worktree, config)
    attempt = checked_path(args.attempt, root)
    if attempt.parent != root / 'attempts':
        raise RuntimeError('MANAGED_ATTEMPT_REQUIRED')
    evidence = checked_path(attempt / 'evidence', root)
    result = read_private(evidence / 'result.json')
    if any(result.get(key) != owner.get(key) for key in ['workspace_id', 'issue_id', 'agent_id']):
        raise RuntimeError('EVIDENCE_OWNER_MISMATCH')
    paths = [evidence / name for name in ['result.json', 'events.jsonl', 'stderr.txt'] if (evidence / name).is_file()]
    paths += [evidence / 'oracle' / name for name in ['result.json', 'cleanup.json'] if (evidence / 'oracle' / name).is_file()]
    # Unique file names let the read-back compare every file, including both result.json files.
    publication = checked_path(attempt / 'publication', root)
    publication.mkdir(exist_ok=True, mode=0o700)
    auth = read_private(Path(config['provider_auth_file']))
    secrets = [config['loginom']['password'], config['loginom']['api_key']]
    for value in auth.values():
        if isinstance(value, dict):
            secrets += [value.get(key, '') for key in ['access', 'refresh', 'key']]
    content_path = checked_path(args.content_file, root)
    content = content_path.read_text()
    files = {}
    for path in paths:
        checked_path(path, root)
        body = path.read_bytes()
        if any(secret and secret.encode() in body for secret in secrets):
            raise RuntimeError('SECRET_IN_EVIDENCE')
        name = ('oracle-' if path.parent.name == 'oracle' else '') + path.name
        target = publication / name
        if target.is_symlink():
            raise RuntimeError('LINKED_PUBLICATION_FILE')
        target.write_bytes(body)
        target.chmod(0o600)
        files[name] = hashlib.sha256(body).hexdigest()
    if any(secret and secret in content for secret in secrets):
        raise RuntimeError('SECRET_IN_COMMENT')
    digest = hashlib.sha256(json.dumps([files, content], sort_keys=True).encode()).hexdigest()
    marker = 'Evidence receipt: ' + digest
    content += '\n\n' + marker
    api, issue = API(), owner['issue_id']
    if api.request('issues/' + issue)['status'] in ['done', 'closed']:
        raise RuntimeError('COMPLETED_ISSUE_NO_NEW_ACTION')
    def find():
        comments = api.request('issues/' + issue + '/comments?full=true')
        return [c for c in comments if marker in c['content']]
    matches = find()
    if not matches:
        command = ['multica', 'issue', 'comment', 'add', issue, '--content-stdin', '--output', 'json']
        if args.parent:
            command += ['--parent', args.parent]
        for name in files:
            command += ['--attachment', str(publication / name)]
        # Even an unknown command outcome must be reconciled by reading persisted comments.
        subprocess.run(command, cwd=args.worktree, input=content, text=True, capture_output=True, timeout=300)
        matches = find()
    if len(matches) != 1:
        raise RuntimeError('EVIDENCE_PUBLICATION_UNCONFIRMED')
    comment = matches[0]
    if not comment['content'].startswith(content):
        raise RuntimeError('COMMENT_CONTENT_MISMATCH')
    verify_upload(api, issue, comment, files)
    write_private(attempt / 'publication-receipt.json', {'verified': True, 'issue_id': issue,
        'comment_id': comment['id'], 'source_sha': result['source_sha'], 'files': files})
    print(json.dumps({'verified': True, 'comment_id': comment['id'], 'source_sha': result['source_sha']}))


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        print(str(error) if isinstance(error, RuntimeError) else 'EVIDENCE_PUBLICATION_FAILED', file=__import__('sys').stderr)
        raise SystemExit(1)
