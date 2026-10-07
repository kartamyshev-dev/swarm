#!/usr/bin/env python3
"""Authorize eight permanent profiles through the unchanged Loginom CLI."""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time
from api import API
from common import read_private, write_private
from concurrency import ensure_idle
from provider_pool import health_path, quarantine, recover, validate_catalog
from shared_auth import auth_lock, private_path

COUNT = 8
DEFAULT_ROOT = Path.home() / '.config/loginom-multica/provider-accounts'


def catalog(root):
    root = Path(root)
    if not root.is_absolute() or root.resolve() != root or '..' in root.parts:
        raise RuntimeError('PROVIDER_PROFILE_ROOT_INVALID')
    if ('multica_workspaces' in root.parts or
            any((parent / '.managed_env.json').exists() for parent in [root, *root.parents])):
        raise RuntimeError('PROVIDER_PROFILES_INSIDE_MANAGED_CHECKOUT')
    return [root / f'{slot:02d}/profile/data/auth.json' for slot in range(1, COUNT + 1)]


def maintenance(api, deployment):
    task_id, agent_id = os.environ.get('MULTICA_TASK_ID'), os.environ.get('MULTICA_AGENT_ID')
    issue_id = deployment.get('maintenance_issue_id')
    if (not task_id or not issue_id or agent_id not in deployment['agents'].values() or
            os.environ.get('MULTICA_WORKSPACE_ID') != deployment['workspace_id']):
        raise RuntimeError('NATIVE_MAINTENANCE_REQUIRED')
    ensure_idle(api, deployment)
    snapshot = api.request('agent-task-snapshot')
    own = [task for task in snapshot if task.get('id') == task_id]
    if (len(own) != 1 or own[0].get('status') != 'running' or
            own[0].get('workspace_id') != deployment['workspace_id'] or
            own[0].get('issue_id') != issue_id or own[0].get('agent_id') != agent_id):
        raise RuntimeError('NATIVE_MAINTENANCE_REQUIRED')


def cli_metadata(cli):
    cli = Path(cli)
    if not cli.is_absolute() or cli.resolve() != cli or cli.name != 'loginom-ai-agent-cli':
        raise RuntimeError('CLI_PATH_INVALID')
    payload = cli.parent.parent
    try:
        value = json.loads((payload / 'cli-manifest.json').read_text())
        metadata = value['metadata']
        if (value['format'] != 'loginom-cli-artifact-v1' or metadata['channel'] not in ['prod', 'beta', 'dev'] or
                metadata['sourceDirty'] is not False or metadata['platform'] != sys.platform or
                metadata['arch'] != {'x86_64': 'x64', 'AMD64': 'x64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine()) or
                not re.fullmatch('[0-9a-f]{40}', metadata['sourceCommit']) or
                not re.fullmatch('[0-9a-f]{64}', metadata['sourceTreeSha256'])):
            raise ValueError()
        files = value['files']
        entries = {entry['path']: entry for entry in files}
        if (len(entries) != len(files) or 'bin/loginom-ai-agent-cli' not in entries or
                not cli.stat().st_mode & 0o111):
            raise ValueError()
        actual = set()
        for path in payload.rglob('*'):
            if path.is_dir() and not path.is_symlink():
                continue
            relative = path.relative_to(payload).as_posix()
            if relative == 'cli-manifest.json':
                continue
            actual.add(relative)
            entry = entries.get(relative)
            info = path.lstat()
            if (not entry or not path.resolve().is_relative_to(payload) or
                    not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode))):
                raise ValueError()
            link = os.readlink(path) if path.is_symlink() else None
            if (stat.S_IMODE(info.st_mode) != entry['mode'] or entry.get('link') != link or
                    hashlib.sha256(link.encode() if link and path.is_dir() else path.read_bytes()).hexdigest() != entry['sha256']):
                raise ValueError()
        if actual != set(entries):
            raise ValueError()
        return metadata
    except (OSError, ValueError, KeyError, TypeError):
        raise RuntimeError('CLI_INTEGRITY_INVALID') from None


def prepare(api, deployment, root, cli):
    maintenance(api, deployment)
    paths = catalog(root)
    metadata = cli_metadata(cli)
    for auth in paths:
        profile = auth.parent.parent
        for directory in [Path(root), profile.parent, profile, profile / 'config', auth.parent,
                          profile / 'state', profile / 'cache', profile / 'cache/tmp', profile / 'loginom']:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            private_path(directory, directory=True)
        marker = profile / 'cli-profile.json'
        expected = {'format': 'loginom-cli', 'version': 1, 'channel': metadata['channel']}
        if marker.exists():
            if read_private(private_path(marker)) != expected:
                raise RuntimeError('PROVIDER_PROFILE_FORMAT_MISMATCH')
        else:
            write_private(marker, expected)
        if not auth.exists():
            fd = os.open(auth, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as stream:
                stream.write('{}\n')
                stream.flush()
                os.fsync(stream.fileno())
        private_path(auth)
    return {'profiles': COUNT, 'provider_auth_files': list(map(str, paths)), 'state': 'prepared'}


def _child(parent):
    # A killed native service must not leave an unlimited device-code poller.
    if sys.platform == 'linux':
        if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
            raise RuntimeError('LOGIN_PARENT_GUARD_FAILED')
        if os.getppid() != parent:
            os.kill(os.getpid(), signal.SIGKILL)


def _visible(line):
    line = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', line)
    if 'https://auth.openai.com/codex/device' in line:
        print('Go to: https://auth.openai.com/codex/device', flush=True)
    code = re.search(r'Enter code:\s*([A-Za-z0-9-]{4,32})(?:\s|$)', line)
    if code:
        print('Enter code: ' + code[1], flush=True)
    if 'Login successful' in line:
        print('Login successful', flush=True)


def _stop(process):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return
    os.killpg(process.pid, signal.SIGKILL)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise RuntimeError('LOGIN_DESCENDANTS_STILL_RUNNING')


def _login_process(cli, profile, fd, channel, timeout):
    env = {key: value for key, value in os.environ.items() if key in
           ['PATH', 'LANG', 'LC_ALL', 'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY',
            'http_proxy', 'https_proxy', 'no_proxy', 'NODE_EXTRA_CA_CERTS', 'SSL_CERT_FILE']}
    env.update({'HOME': str(profile), 'LOGINOM_AI_AGENT_CLI_PROFILE': str(profile),
                'LOGINOM_AI_AGENT_CHANNEL': channel, 'LOGINOM_AI_AGENT_PURE': '1',
                'LOGINOM_AI_AGENT_DISABLE_PROJECT_CONFIG': '1'})
    parent = os.getpid()
    process = subprocess.Popen([str(cli), 'providers', 'login', '--provider', 'openai', '--method',
                                'ChatGPT Pro/Plus (headless)'], cwd=profile, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
                               pass_fds=(fd,), preexec_fn=lambda: _child(parent))
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    pending, deadline = '', time.monotonic() + timeout
    try:
        while selector.get_map():
            if time.monotonic() >= deadline:
                raise RuntimeError('PROVIDER_LOGIN_TIMED_OUT')
            for key, _ in selector.select(min(0.2, max(0, deadline - time.monotonic()))):
                chunk = os.read(key.fd, 4096)
                if not chunk:
                    selector.unregister(key.fileobj)
                    _visible(pending)
                    break
                pending += chunk.decode('utf-8', errors='replace')
                lines = re.split('[\r\n]', pending)
                pending = lines.pop()[-4096:]
                for line in lines:
                    _visible(line)
        try:
            code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise RuntimeError('PROVIDER_LOGIN_TIMED_OUT') from None
        if code:
            raise RuntimeError('PROVIDER_LOGIN_FAILED')
    finally:
        selector.close()
        _stop(process)
        process.stdout.close()


def _clean_profile(profile):
    # No model or Loginom run belongs to this authorization-only profile.
    for path in [profile / 'config', profile / 'state', profile / 'cache', profile / 'loginom']:
        private_path(path, directory=True)
        for entry in path.iterdir():
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
    (profile / 'cache/tmp').mkdir(mode=0o700, exist_ok=True)
    for entry in (profile / 'data').iterdir():
        if entry.name in ['auth.json', 'auth.json.lock', 'auth.json.health.json']:
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()


def login(api, deployment, root, cli, slot, *, timeout=900):
    if type(slot) is not int or not 1 <= slot <= COUNT or timeout <= 0:
        raise RuntimeError('PROVIDER_LOGIN_ARGUMENT_INVALID')
    maintenance(api, deployment)
    metadata = cli_metadata(cli)
    auth = catalog(root)[slot - 1]
    profile = private_path(auth.parent.parent, directory=True)
    expected = {'format': 'loginom-cli', 'version': 1, 'channel': metadata['channel']}
    if read_private(private_path(profile / 'cli-profile.json')) != expected:
        raise RuntimeError('PROVIDER_PROFILE_FORMAT_MISMATCH')
    fd = auth_lock(auth, nonblocking=True)
    try:
        if (profile / '.writer').exists():
            raise RuntimeError('PROVIDER_PROFILE_WRITER_RECOVERY_REQUIRED')
        quarantine(auth, 'PROVIDER_LOGIN_IN_PROGRESS', fd=fd)
        try:
            _login_process(cli, profile, fd, metadata['channel'], timeout)
        except BaseException as error:
            if str(error) != 'LOGIN_DESCENDANTS_STILL_RUNNING':
                _clean_profile(profile)
            raise
        if (profile / '.writer').exists():
            raise RuntimeError('PROVIDER_PROFILE_WRITER_RECOVERY_REQUIRED')
        maintenance(api, deployment)
        _clean_profile(profile)
        recover(auth, fd=fd)
        return {'slot': slot, 'state': 'authorized', 'provider_auth_file': str(auth)}
    finally:
        os.close(fd)


def check(root):
    paths = catalog(root)
    validate_catalog(list(map(str, paths)), expected_count=COUNT)
    if any(health_path(auth).exists() for auth in paths):
        raise RuntimeError('PROVIDER_AUTH_RECOVERY_REQUIRED')
    for auth in paths:
        profile = auth.parent.parent
        marker = read_private(private_path(profile / 'cli-profile.json'))
        if (marker.get('format') != 'loginom-cli' or marker.get('version') != 1 or
                marker.get('channel') not in ['prod', 'beta', 'dev'] or (profile / '.writer').exists()):
            raise RuntimeError('PROVIDER_PROFILE_FORMAT_MISMATCH')
    return {'profiles': COUNT, 'state': 'ready', 'provider_auth_files': list(map(str, paths))}


def activate(api, deployment, root, operator, cards):
    maintenance(api, deployment)
    operator = private_path(operator)
    value = read_private(operator)
    if value.get('workspace_id') != deployment['workspace_id']:
        raise RuntimeError('WORKSPACE_MISMATCH')
    configs = [operator]
    if Path(cards).exists():
        for card in private_path(cards, directory=True).iterdir():
            private_path(card, directory=True)
            for role in ['worker', 'reviewer']:
                path = card / (role + '.json')
                if not path.exists():
                    continue
                data = read_private(private_path(path))
                if (data.get('operator_file') != str(operator) or
                        data.get('workspace_id') != deployment['workspace_id'] or
                        data.get('issue_id') != card.name or
                        data.get('agent_id') != deployment['agents'][role] or data.get('role') != role):
                    raise RuntimeError('FOREIGN_ROLE_CONFIG')
                stage = data.get('stage', 'full')
                if stage not in ['full', 'stage0']:
                    raise RuntimeError('ROLE_STAGE_INVALID')
                if stage == 'full':
                    configs.append(path)
    state = check(root)
    for path in configs:
        maintenance(api, deployment)
        data = read_private(path)
        data.pop('provider_auth_file', None)
        write_private(path, {**data, 'provider_auth_files': state['provider_auth_files']})
    if any(read_private(path).get('provider_auth_files') != state['provider_auth_files'] or
           'provider_auth_file' in read_private(path) for path in configs):
        raise RuntimeError('PROVIDER_AUTH_ACTIVATION_READBACK_FAILED')
    return {**state, 'state': 'activated', 'configs': len(configs), 'readback': 'verified'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['prepare', 'login', 'check', 'activate'])
    parser.add_argument('--deployment', type=Path, required=True)
    parser.add_argument('--root', type=Path, default=DEFAULT_ROOT)
    parser.add_argument('--cli', type=Path)
    parser.add_argument('--slot', type=int)
    parser.add_argument('--timeout', type=float, default=900)
    parser.add_argument('--operator', type=Path, default=Path.home() / '.config/loginom-multica/operator.json')
    parser.add_argument('--cards', type=Path, default=Path.home() / '.config/loginom-multica/cards')
    args = parser.parse_args()
    deployment = json.loads(args.deployment.read_text())
    if args.command == 'check':
        result = check(args.root)
    elif args.command == 'prepare':
        if args.cli is None:
            parser.error('--cli is required for prepare')
        result = prepare(API(), deployment, args.root, args.cli)
    elif args.command == 'login':
        if args.cli is None or args.slot is None:
            parser.error('--cli and --slot are required for login')
        result = login(API(), deployment, args.root, args.cli, args.slot, timeout=args.timeout)
    else:
        result = activate(API(), deployment, args.root, args.operator, args.cards)
    print(json.dumps(result))


if __name__ == '__main__':
    def cancelled(_signum, _frame):
        raise RuntimeError('PROVIDER_LOGIN_CANCELLED')
    signal.signal(signal.SIGTERM, cancelled)
    signal.signal(signal.SIGINT, cancelled)
    try:
        main()
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        # Auth/config/CLI response bodies are never part of a diagnostic.
        raise SystemExit(str(error) if isinstance(error, RuntimeError) else 'PROVIDER_AUTH_MAINTENANCE_FAILED')
