import importlib.util
import base64
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import common
import linux
import provider_pool
import shared_auth
import accept


def token(user, variant=''):
    payload = base64.urlsafe_b64encode(json.dumps({'https://api.openai.com/auth': {'chatgpt_user_id': user, 'chatgpt_account_id': 'shared-workspace'}, 'variant': variant}).encode()).decode().rstrip('=')
    return 'header.' + payload + '.signature'


class ProviderPoolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.paths = []
        self.children = []
        for index in range(8):
            directory = self.root / str(index) / 'data'
            directory.mkdir(parents=True, mode=0o700)
            directory.parent.chmod(0o700)
            path = directory / 'auth.json'
            common.write_private(path, {'openai': {'type': 'oauth', 'access': token(str(index)),
                                                   'refresh': 'refresh-' + str(index), 'expires': 0}})
            self.paths.append(str(path))

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            for stream in [child.stdin, child.stdout, child.stderr]:
                if stream:
                    stream.close()
        self.temporary.cleanup()

    def acquire(self, **kwargs):
        return provider_pool.acquire(self.paths, deadline=time.monotonic() + 3, owner='test', **kwargs)

    def child(self, code, *args):
        child = subprocess.Popen([sys.executable, '-c', code, str(SCRIPTS), *map(str, args)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(child)
        return child

    def line(self, child):
        ready, _, _ = select.select([child.stdout], [], [], 5)
        if not ready:
            self.fail('child produced no readiness signal')
        value = child.stdout.readline().strip()
        if not value:
            self.fail(child.stderr.read())
        return value

    def test_eight_real_processes_use_distinct_slots_and_ninth_waits(self):
        code = """import sys,time,json
sys.path.insert(0,sys.argv[1])
from provider_pool import acquire
lease=acquire(json.loads(sys.argv[2]),deadline=time.monotonic()+8,owner='child')
print(lease.path,flush=True)
sys.stdin.readline()
lease.close(completed=True)
"""
        children = [self.child(code, json.dumps(self.paths)) for _ in range(8)]
        selected = [self.line(child) for child in children]
        self.assertEqual(len(set(selected)), 8)
        ninth = self.child(code, json.dumps(self.paths))
        self.assertEqual(select.select([ninth.stdout], [], [], 0.15)[0], [])
        children[0].stdin.write('\n'); children[0].stdin.flush()
        children[0].wait(timeout=3)
        self.assertEqual(self.line(ninth), selected[0])
        for child in children[1:] + [ninth]:
            child.stdin.write('\n'); child.stdin.flush()
            self.assertEqual(child.wait(timeout=3), 0)

    def test_killed_owner_cannot_release_live_child_lock(self):
        code = """import sys,time,json,subprocess
sys.path.insert(0,sys.argv[1])
from provider_pool import acquire
lease=acquire(json.loads(sys.argv[2]),deadline=time.monotonic()+3,owner='parent')
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(10)'],pass_fds=(lease.fd,))
print(json.dumps([str(lease.path),child.pid]),flush=True)
sys.stdin.readline()
"""
        parent = self.child(code, json.dumps(self.paths))
        path, child_pid = json.loads(self.line(parent))
        try:
            parent.kill(); parent.wait(timeout=3)
            with self.assertRaisesRegex(RuntimeError, 'AUTH_BUSY'):
                shared_auth.auth_lock(path, nonblocking=True)
        finally:
            os.kill(child_pid, signal.SIGTERM)
        for _ in range(100):
            try:
                fd = shared_auth.auth_lock(path, nonblocking=True)
                os.close(fd)
                break
            except RuntimeError:
                time.sleep(0.02)
        else:
            self.fail('child lock did not release')
        lease = self.acquire()
        self.assertNotEqual(str(lease.path), path)
        self.assertEqual(common.read_private(provider_pool.health_path(path))['reason'], 'PROVIDER_AUTH_INTERRUPTED')
        lease.close(completed=True)

    def test_missing_profile_directory_and_damaged_lock_do_not_disable_healthy_slots(self):
        shutil.rmtree(Path(self.paths[0]).parent)
        fd = shared_auth.auth_lock(self.paths[1]); os.close(fd)
        Path(self.paths[1] + '.lock').chmod(0o644)
        lease = self.acquire()
        self.assertEqual(str(lease.path), self.paths[2])
        lease.close(completed=True)

    def test_wait_deadline_and_cancel_are_bounded(self):
        leases = [self.acquire() for _ in range(8)]
        try:
            start = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, 'ATTEMPT_DEADLINE_EXCEEDED'):
                provider_pool.acquire(self.paths, deadline=start + 0.15, owner='ninth')
            self.assertLess(time.monotonic() - start, 0.5)
            with self.assertRaisesRegex(RuntimeError, 'AUTH_WAIT_CANCELLED'):
                self.acquire(cancelled=lambda: True)
        finally:
            for lease in leases:
                lease.close(completed=True)

    def test_catalog_rejects_copies_links_and_permissions(self):
        provider_pool.validate_catalog(self.paths)
        with self.assertRaisesRegex(RuntimeError, 'DUPLICATE'):
            provider_pool.validate_catalog([self.paths[0], *self.paths[1:7], self.paths[0]])
        original = Path(self.paths[1]).read_bytes()
        Path(self.paths[1]).write_bytes(Path(self.paths[0]).read_bytes())
        with self.assertRaisesRegex(RuntimeError, 'DUPLICATE'):
            provider_pool.validate_catalog(self.paths)
        Path(self.paths[1]).write_bytes(original)
        Path(self.paths[1]).unlink(); Path(self.paths[1]).symlink_to(self.paths[0])
        with self.assertRaisesRegex(RuntimeError, 'AUTH_PATH_INVALID'):
            provider_pool.validate_catalog(self.paths)
        Path(self.paths[1]).unlink(); os.link(self.paths[0], self.paths[1])
        with self.assertRaisesRegex(RuntimeError, 'AUTH_PATH_INVALID'):
            provider_pool.validate_catalog(self.paths)

    def test_same_user_with_different_tokens_is_rejected_shared_workspace_is_allowed(self):
        provider_pool.validate_catalog(self.paths)
        common.write_private(self.paths[1], {'openai': {'type': 'oauth', 'access': token('0', 'another-login'), 'refresh': 'different-refresh', 'expires': 0}})
        with self.assertRaisesRegex(RuntimeError, 'DUPLICATE_USER'):
            provider_pool.validate_catalog(self.paths)

    def test_invalid_and_quarantined_slots_are_skipped_until_recovery(self):
        Path(self.paths[0]).write_text('{broken')
        lease = self.acquire()
        self.assertEqual(str(lease.path), self.paths[1])
        lease.quarantine('PROVIDER_AUTH_REVOKED'); lease.close(completed=True)
        next_lease = self.acquire()
        self.assertEqual(str(next_lease.path), self.paths[2])
        next_lease.close(completed=True)
        common.write_private(self.paths[0], {'openai': {'type': 'oauth', 'access': 'repaired-access', 'refresh': 'repaired-refresh', 'expires': 0}})
        provider_pool.recover(self.paths[0])
        final = self.acquire()
        self.assertEqual(str(final.path), self.paths[0])
        final.close(completed=True)

    def test_missing_or_wrong_permission_slot_does_not_disable_other_accounts(self):
        Path(self.paths[0]).unlink()
        Path(self.paths[1]).chmod(0o644)
        lease = self.acquire()
        self.assertEqual(str(lease.path), self.paths[2])
        self.assertEqual(common.read_private(provider_pool.health_path(self.paths[0]))['reason'], 'PROVIDER_AUTH_INVALID')
        self.assertEqual(common.read_private(provider_pool.health_path(self.paths[1]))['reason'], 'PROVIDER_AUTH_INVALID')
        lease.close(completed=True)

    def test_model_error_never_retries_same_attempt_under_another_account(self):
        for index, error in enumerate(['quota exceeded', 'Token refresh failed: HTTP 401 invalid_grant']):
            with self.subTest(error=error):
                worktree = self.root / ('work-' + str(index)); worktree.mkdir()
                root = worktree / '.multica-node'; root.mkdir()
                current = root / 'current'; current.mkdir()
                common.write_private(current / 'cli-manifest.json', {'metadata': {'channel': 'dev', 'sourceDirty': False, 'sourceCommit': 'commit', 'sourceTreeSha256': 'tree'}})
                acceptance = worktree / 'docs/node-development/nodes/test/acceptance'
                (acceptance / 'data').mkdir(parents=True)
                (acceptance / 'task.md').write_text('Save {{PACKAGE_PATH}}')
                config_path = self.root / ('role-' + str(index) + '.json')
                config = {'role': 'worker', 'model': 'openai/test', 'variant': 'low', 'provider_auth_files': self.paths,
                          'loginom_lock_dir': str(self.root / 'account-locks'),
                          'loginom': {'url': 'https://loginom.invalid', 'username': 'worker', 'password': 'private', 'api_key': 'private'}}
                common.write_private(config_path, config)
                out = root / 'attempts/fail'
                args = ['accept.py', '--worktree', str(worktree), '--node', 'test', '--config', str(config_path), '--cli', str(current / 'bin/loginom-ai-agent-cli'), '--out', str(out)]
                calls = []
                def run(command, **kwargs):
                    calls.append((command, kwargs['auth']))
                    kwargs['stdout'].write((json.dumps({'error': error}) + '\n').encode())
                    return 0 if 'setup' in command else 1
                def redact(command, **kwargs):
                    (out / 'evidence/events.jsonl').write_text('sanitized\n')
                    (out / 'evidence/stderr.txt').write_text('')
                    return SimpleNamespace(returncode=0)
                with patch.object(sys, 'argv', args), patch.object(accept, 'managed_root', return_value=(root, {})), \
                     patch.object(accept, 'verify_candidate', return_value=SimpleNamespace(returncode=0)), \
                     patch.object(accept, 'ops_identity', return_value={'commit': 'fixture'}), \
                     patch.object(accept, 'run', side_effect=run), patch.object(accept.subprocess, 'run', side_effect=redact), redirect_stdout(StringIO()):
                    self.assertEqual(accept.main(), 1)
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[0][1], calls[1][1])
                self.assertEqual(str(calls[1][1]), self.paths[0])
                result = common.read_private(out / 'evidence/result.json')
                self.assertEqual(result['cli_exit'], 1)
                self.assertTrue(result['redaction_complete'])
                self.assertEqual(set(result['redacted_files']), {'events.jsonl', 'stderr.txt'})
                next_lease = self.acquire()
                self.assertEqual(str(next_lease.path), self.paths[0 if index == 0 else 1])
                next_lease.close(completed=True)

    def test_in_place_refresh_persists_and_attempt_cleanup_preserves_auth(self):
        lease = self.acquire()
        identity = lease.path.stat().st_ino
        with lease.path.open('w') as stream:
            json.dump({'openai': {'type': 'oauth', 'access': 'new-access', 'refresh': 'new-refresh', 'expires': 999}}, stream)
        self.assertEqual(lease.path.stat().st_ino, identity)
        self.assertEqual(lease.current_secrets(), ['new-access', 'new-refresh'])
        self.assertEqual(lease.secrets, [token('0'), 'refresh-0'])
        lease.close(completed=True)
        attempt = self.root / 'managed/attempt'; attempt.mkdir(parents=True)
        shutil.rmtree(attempt.parent)
        next_lease = self.acquire()
        self.assertEqual(next_lease.secrets, ['new-access', 'new-refresh'])
        next_lease.close(completed=True)

    def test_borrowed_launcher_auth_fd_is_not_reacquired_or_closed(self):
        lease = self.acquire()
        attempt = self.root / 'attempt'; (attempt / 'profile/data').mkdir(parents=True)
        observed = []
        class Process:
            returncode = 0
            def __init__(self, argv, **kwargs): observed.append((argv, kwargs))
            def communicate(self, *args, **kwargs): pass
            def poll(self): return 0
        with patch.object(linux.subprocess, 'Popen', Process), patch.object(linux, 'auth_lock', side_effect=AssertionError('reacquired')):
            linux.run(['fake-cli'], attempt=attempt, payload=self.root, cwd=attempt, profile=attempt / 'profile', auth=lease.path, auth_fd=lease.fd)
        self.assertEqual(observed[0][1]['pass_fds'], (lease.fd,))
        self.assertIn('LOGINOM_MULTICA_LEASE_FDS', observed[0][0])
        self.assertIn(str(lease.path), observed[0][0])
        self.assertNotIn(self.paths[1], observed[0][0])
        os.fstat(lease.fd)
        lease.close(completed=True)

    def test_stable_loginom_account_lock_blocks_other_worktree_and_cleanup(self):
        config = {'loginom_lock_dir': str(self.root / 'accounts'), 'loginom': {'url': 'https://loginom.invalid', 'username': 'same-user'}}
        fd = provider_pool.loginom_lock(config, deadline=time.monotonic() + 1)
        try:
            self.assertEqual(provider_pool.inherited_account_lock(config, fd), fd)
            with self.assertRaisesRegex(RuntimeError, 'ATTEMPT_DEADLINE_EXCEEDED'):
                provider_pool.loginom_lock(config, deadline=time.monotonic() + 0.1)
            other = {**config, 'loginom': {**config['loginom'], 'username': 'other-user'}}
            os.close(provider_pool.loginom_lock(other, deadline=time.monotonic() + 1))
        finally:
            os.close(fd)

    def test_headed_entry_forwards_lease_to_real_command(self):
        lease = self.acquire()
        code = "import os,sys;print(os.fstat(int(sys.argv[1])).st_ino)"
        spec = importlib.util.spec_from_file_location('headed_entry_test', SCRIPTS / 'headed-entry.py')
        module = importlib.util.module_from_spec(spec)
        actual = subprocess.run
        original_popen = subprocess.Popen
        results = []
        class WindowManager:
            def poll(self): return None
            def terminate(self): pass
            def wait(self, **kwargs): return 0
        def run(command, **kwargs):
            if command[0] == '/usr/bin/xprop':
                return subprocess.CompletedProcess(command, 0, 'window id')
            result = actual(command, capture_output=True, text=True, **kwargs)
            results.append(result)
            return result
        with patch.dict(os.environ, {'LOGINOM_MULTICA_LEASE_FDS': str(lease.fd)}), \
             patch.object(sys, 'argv', ['headed-entry.py', sys.executable, '-c', code, str(lease.fd)]), \
             patch.object(subprocess, 'Popen', return_value=WindowManager()) as popen:
            # Keep the actual subprocess implementation for the tested command.
            def start(command, **kwargs):
                return WindowManager() if command[0] == '/usr/bin/openbox' else original_popen(command, **kwargs)
            popen.side_effect = start
            with patch.object(subprocess, 'run', side_effect=run):
                with self.assertRaises(SystemExit) as exit:
                    spec.loader.exec_module(module)
        self.assertEqual(exit.exception.code, 0)
        self.assertEqual(results[0].stdout.strip(), str(os.fstat(lease.fd).st_ino))
        lease.close(completed=True)

    def test_redaction_removes_pre_and_post_refresh_values(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node required for the production redactor boundary')
        redactor = self.root / 'redactor.mjs'
        redactor.write_text("export function createRedactor(secrets) { return { text: value => secrets.reduce((text,secret) => text.replaceAll(secret,'[redacted]'),value), redact(value) { return JSON.parse(this.text(JSON.stringify(value))) } } }")
        config = self.root / 'role.json'
        common.write_private(config, {'loginom': {'password': 'loginom-password', 'api_key': 'loginom-key'}})
        common.write_private(self.paths[0], {'openai': {'access': 'new-access', 'refresh': 'new-refresh'}})
        stdout, stderr, evidence = self.root / 'stdout', self.root / 'stderr', self.root / 'evidence'
        evidence.mkdir()
        (evidence / 'oracle').mkdir()
        raw = 'old-access old-refresh new-access new-refresh loginom-password loginom-key'
        stdout.write_text(json.dumps({'text': raw}) + '\n'); stderr.write_text(raw)
        for name in ['result.json', 'cleanup.json']:
            (evidence / 'oracle' / name).write_text(json.dumps({'text': raw}))
        result = subprocess.run([node, SCRIPTS / 'redact.mjs', redactor, config, self.paths[0], stdout, stderr, evidence, '--secrets-stdin'],
                                input=json.dumps(['old-access', 'old-refresh']), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        cleaned = (evidence / 'events.jsonl').read_text() + (evidence / 'stderr.txt').read_text() + ''.join((evidence / 'oracle' / name).read_text() for name in ['result.json', 'cleanup.json'])
        for secret in raw.split():
            self.assertNotIn(secret, cleaned)


if __name__ == '__main__':
    unittest.main()
