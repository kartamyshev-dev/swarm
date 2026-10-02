#!/usr/bin/env python3
"""Run CLI and the client's independent oracle; preserve failed evidence."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from common import artifact_lock, checked_path, managed_root, ops_identity, read_private, verify_candidate, write_private
from linux import run


def passed(result):
    return (result['cli_exit'] == 0 and result['oracle_exit'] == 0 and
            result['oracle']['status'] == 'PASS' and not result['timed_out'] and
            result['cleanup']['package_closed'] and result['cleanup']['logged_out'])


def prepare_profile(profile, channel, cache):
    profile.mkdir(mode=0o700)
    write_private(profile / 'cli-profile.json', {'format': 'loginom-cli', 'version': 1, 'channel': channel})
    for name in ['config', 'data', 'state', 'cache', 'tmp', 'loginom']:
        (profile / name).mkdir(mode=0o700)
    if cache and Path(cache).is_file():
        shutil.copyfile(cache, profile / 'cache/models.json')
        (profile / 'cache/models.json').chmod(0o600)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--worktree', required=True, type=Path)
    parser.add_argument('--node', required=True)
    parser.add_argument('--config', default=os.environ.get('LOGINOM_MULTICA_CONFIG'), type=Path)
    parser.add_argument('--cli', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    if not args.config:
        raise RuntimeError('ROLE_CONFIG_REQUIRED')
    config = read_private(args.config)
    root, owner = managed_root(args.worktree, config)
    output = checked_path(args.out, root)
    if output.parent != root / 'attempts' or output.exists():
        raise RuntimeError('NEW_MANAGED_ATTEMPT_REQUIRED')
    payload = checked_path(root / 'current', root)
    if args.cli != payload / 'bin/loginom-ai-agent-cli':
        raise RuntimeError('CLI_MUST_BE_CURRENT_CANDIDATE')
    lock = artifact_lock(root)
    if verify_candidate(args.worktree, payload).returncode:
        os.close(lock)
        raise RuntimeError('CANDIDATE_INVALID_REBUILD_REQUIRED')
    manifest = json.loads((payload / 'cli-manifest.json').read_text())
    metadata = manifest['metadata']
    if metadata['sourceDirty']:
        os.close(lock)
        raise RuntimeError('COMMITTED_SOURCE_REQUIRED')
    acceptance = args.worktree / 'docs/node-development/nodes' / args.node / 'acceptance'
    if acceptance.resolve() != acceptance or not (acceptance / 'task.md').is_file():
        os.close(lock)
        raise RuntimeError('NODE_ACCEPTANCE_MISSING')
    output.parent.mkdir(exist_ok=True, mode=0o700)
    output.mkdir(mode=0o700)
    evidence, temporary = output / 'evidence', output / 'tmp'
    evidence.mkdir(mode=0o700)
    temporary.mkdir(mode=0o700)
    work = temporary / 'work'
    work.mkdir(mode=0o700)
    profile = temporary / 'profile'
    prepare_profile(profile, metadata['channel'], config.get('model_cache_file'))
    resources = payload / 'resources/loginom'
    node = resources / 'bin/node'
    package = '/' + config['loginom']['username'] + '/node-' + args.node + '-' + output.name + '.lgp'
    task = (acceptance / 'task.md').read_text()
    if '{{PACKAGE_PATH}}' not in task:
        raise RuntimeError('PACKAGE_PLACEHOLDER_REQUIRED')
    (work / 'task.md').write_text(task.replace('{{PACKAGE_PATH}}', package))
    for source in (acceptance / 'data').iterdir():
        if source.is_file() and source.name not in ['README', 'README.md']:
            shutil.copyfile(source, work / source.name)
    result = {'status': 'FAIL', 'node': args.node, 'role': config['role'], **owner,
              'source_sha': metadata['sourceCommit'], 'source_tree_sha256': metadata['sourceTreeSha256'],
              'cli_manifest_sha256': __import__('hashlib').sha256((payload / 'cli-manifest.json').read_bytes()).hexdigest(),
              'ops': ops_identity(), 'model': config['model'], 'variant': config['variant'],
              'cli_exit': None, 'oracle_exit': None, 'timed_out': False, 'package_path': package,
              'oracle': {'status': 'not_run'}, 'cleanup': {'package_closed': False, 'logged_out': False}}
    started = time.monotonic()
    raw_stdout, raw_stderr = temporary / 'stdout.raw', temporary / 'stderr.raw'
    launcher = dict(attempt=output, payload=payload, profile=profile, cwd=work, auth=Path(config['provider_auth_file']), capabilities=metadata.get('capabilities', []), pass_fds=(lock,))
    try:
        setup = {'url': config['loginom']['url'], 'username': config['loginom']['username'], 'password': config['loginom']['password'], 'apiKey': config['loginom']['api_key']}
        with raw_stdout.open('wb') as stdout, raw_stderr.open('wb') as stderr:
            code = run([args.cli, 'loginom', 'setup', '--stdin-json', '--format', 'json'], **launcher, input=json.dumps(setup).encode(), stdout=stdout, stderr=stderr, timeout=240)
            if code:
                raise RuntimeError('CLI_SETUP_FAILED')
        # Model list/check is done in bootstrap; the explicit run itself proves availability.
        with raw_stdout.open('wb') as stdout, raw_stderr.open('wb') as stderr:
            command = [args.cli, 'run', '--no-headless', '--format', 'json', '--model', config['model'], '--variant', config['variant'], '--dir', work]
            for file in sorted(work.iterdir()):
                command += ['--file', file]
            command += ['--', 'Выполни приложенное задание и сохрани результат в указанном новом пакете без перезаписи существующего файла.']
            result['cli_exit'] = run(command, **launcher, stdout=stdout, stderr=stderr, timeout=7200)
        # Expectations and administrator settings are created only after the model process exits.
        expected = json.loads((acceptance / 'expected.json').read_text())
        expected['package_path'] = package
        write_private(temporary / 'expected.json', expected)
        write_private(temporary / 'saved.json', {'path': package})
        cold_config = {'api_key': setup['apiKey'], 'loginom_url': setup['url'], 'workflow_profile': {'passwordless_login': setup['password'] == '', 'loginom_user': setup['username'], 'password': setup['password']}}
        write_private(temporary / 'cold-config.json', cold_config)
        operator_path = Path(config['operator_file'])
        operator = read_private(operator_path)
        write_private(temporary / 'admin.json', {key: operator[key] for key in ['url', 'admin_user', 'admin_password']})
        release = temporary / 'release'
        release.mkdir(mode=0o700)
        with (release / 'stdout').open('wb') as stdout, (release / 'stderr').open('wb') as stderr:
            code = run([node, Path(__file__).with_name('release-sessions.mjs'), '--resources', resources, '--accounts', temporary / 'admin.json', '--user', setup['username'], '--output', release], attempt=output, payload=payload, cwd=release, stdout=stdout, stderr=stderr, timeout=180, pass_fds=(lock,))
            if code:
                raise RuntimeError('OWN_SESSION_RELEASE_FAILED')
        oracle = evidence / 'oracle'
        oracle.mkdir(mode=0o700)
        with (oracle / 'stdout.txt').open('wb') as stdout, (oracle / 'stderr.txt').open('wb') as stderr:
            result['oracle_exit'] = run([node, args.worktree / 'scripts/node-acceptance/cold-check.mjs', '--config', temporary / 'cold-config.json', '--resources', resources, '--saved', temporary / 'saved.json', '--expected', temporary / 'expected.json', '--output', oracle], attempt=output, payload=payload, cwd=oracle, read_only=[args.worktree], stdout=stdout, stderr=stderr, timeout=600, pass_fds=(lock,))
        cold_result = json.loads((oracle / 'result.json').read_text())
        result['oracle'] = {'status': cold_result.get('status', 'FAIL')}
        result['cleanup'] = cold_result.get('cleanup', result['cleanup'])
        result['status'] = 'PASS' if passed(result) else 'FAIL'
    except RuntimeError as error:
        result['error'] = str(error)
        result['timed_out'] = str(error) == 'COMMAND_TIMED_OUT'
    except (OSError, ValueError, subprocess.CalledProcessError):
        result['error'] = 'ACCEPTANCE_COMMAND_FAILED'
    finally:
        # Preserve sanitized setup/timeout failures too; raw logs never become attachments.
        if raw_stdout.exists() and raw_stderr.exists():
            try:
                subprocess.run([node, Path(__file__).with_name('redact.mjs'), args.worktree / 'packages/loginom-runtime/client/lib/redact.mjs', args.config, config['provider_auth_file'], raw_stdout, raw_stderr, evidence], check=True, capture_output=True)
            except (OSError, subprocess.CalledProcessError):
                result['status'] = 'FAIL'
                result['evidence_error'] = 'REDACTION_FAILED'
        result['duration_s'] = round(time.monotonic() - started, 3)
        write_private(evidence / 'result.json', result)
        # The entire attempt is retained for native GC; secret files are never attached.
        os.close(lock)
    print(json.dumps({'status': result['status'], 'result': str(evidence / 'result.json'), 'source_sha': result['source_sha']}))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(str(error), file=__import__('sys').stderr)
        raise SystemExit(1)
