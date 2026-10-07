import fcntl
import importlib.util
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import common
import concurrency
import configure
import linux
import shared_auth

spec = importlib.util.spec_from_file_location('prepare_parallel_auth', SCRIPTS / 'prepare-parallel-auth.py')
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


class FakeAPI:
    def __init__(self):
        self.agents = {role: {'id': role, 'max_concurrent_tasks': 1,
                            'model': 'keep', 'mcp_config': {'keep': True}}
                       for role in ['generator', 'worker', 'reviewer']}
        self.tasks = []
        self.writes = []

    def request(self, path, data=None):
        if path == 'agent-task-snapshot':
            return self.tasks
        agent = self.agents[path.removeprefix('agents/')]
        if data is not None:
            self.writes.append((path, data))
            agent.update(data)
        return agent


class ParallelTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.api = FakeAPI()
        self.deployment = {'workspace_id': 'workspace', 'agents': {role: role for role in self.api.agents}}
        self.auth = self.root / 'auth.json'
        common.write_private(self.auth, {'provider': {'type': 'oauth', 'refresh': 'fake'}})
        self.operator = self.root / 'operator.json'
        common.write_private(self.operator, {'workspace_id': 'workspace', 'provider_auth_file': str(self.auth)})
        self.cards = self.root / 'cards'
        self.cards.mkdir(mode=0o700)
        self.role = self.cards / 'issue/worker.json'
        common.write_private(self.role, {'provider_auth_file': str(self.auth), 'operator_file': str(self.operator),
                                        'workspace_id': 'workspace', 'issue_id': 'issue', 'agent_id': 'worker', 'role': 'worker'})

    def tearDown(self):
        self.temporary.cleanup()

    def migrate(self):
        return migration.migrate(self.api, self.deployment, self.operator, self.cards, source=self.auth)

    def test_migration_moves_token_and_lock_then_reuses_identity(self):
        lock = shared_auth.auth_lock(self.auth)
        inode = os.fstat(lock).st_ino
        os.close(lock)
        result = self.migrate()
        target = Path(result['provider_auth_file'])
        self.assertFalse(self.auth.exists())
        self.assertFalse(Path(str(self.auth) + '.lock').exists())
        self.assertEqual(Path(str(target) + '.lock').stat().st_ino, inode)
        self.assertEqual(common.read_private(self.role)['provider_auth_file'], str(target))
        self.assertEqual(self.migrate(), result)
        self.assertEqual(shared_auth.auth_mode(target, ['shared-oauth-v1']), ('shared-refresh', target.parent))
        self.assertEqual(shared_auth.auth_mode(target, []), ('serial', target.parent))

    def test_migration_failure_resumes_without_copy_or_stale_reference(self):
        actual_write = migration.write_private
        def fail_role(path, data):
            if path == self.role:
                raise OSError('simulated interruption')
            actual_write(path, data)
        with patch.object(migration, 'write_private', side_effect=fail_role):
            with self.assertRaises(OSError):
                self.migrate()
        target = self.root / 'provider-auth/auth.json'
        self.assertTrue(target.exists())
        self.assertFalse(self.auth.exists())
        with self.assertRaisesRegex(RuntimeError, 'AUTH_MIGRATION_INCOMPLETE'):
            shared_auth.auth_mode(target, ['shared-oauth-v1'])
        self.migrate()
        self.assertEqual(common.read_private(self.role)['provider_auth_file'], str(target))

    def test_live_auth_lock_prevents_migration(self):
        lock = shared_auth.auth_lock(self.auth)
        try:
            with self.assertRaisesRegex(RuntimeError, 'AUTH_BUSY'):
                self.migrate()
            self.assertTrue(self.auth.is_file())
        finally:
            os.close(lock)
        self.migrate()

    def test_queued_or_running_task_prevents_any_update(self):
        for state in ['queued', 'dispatched', 'running', 'waiting_local_directory', 'unknown']:
            self.api.tasks = [{'agent_id': 'worker', 'status': state}]
            with self.assertRaisesRegex(RuntimeError, 'WORKSPACE_NOT_IDLE'):
                concurrency.set_limits(self.api, self.deployment, 2)
            with self.assertRaisesRegex(RuntimeError, 'WORKSPACE_NOT_IDLE'):
                self.migrate()
            self.assertEqual(self.api.writes, [])
            self.assertTrue(self.auth.exists())

    def test_limits_update_only_concurrency_and_check_readback(self):
        limits = concurrency.set_limits(self.api, self.deployment, 2)
        self.assertEqual(limits, {'generator': 1, 'worker': 2, 'reviewer': 2})
        self.assertTrue(all(set(data) == {'max_concurrent_tasks'} for _, data in self.api.writes))
        self.assertTrue(all(a['model'] == 'keep' and a['mcp_config'] == {'keep': True} for a in self.api.agents.values()))

    def test_mismatched_limit_readback_is_not_success(self):
        original = self.api.request
        def ignored_write(path, data=None):
            return original(path)
        with patch.object(self.api, 'request', side_effect=ignored_write):
            with self.assertRaisesRegex(RuntimeError, 'CONCURRENCY_READBACK_MISMATCH'):
                concurrency.set_limits(self.api, self.deployment, 2)

    def test_configure_retains_explicit_per_role_limits(self):
        instructions = self.root / 'instructions'; instructions.mkdir()
        for name in ['generator', 'worker', 'reviewer', 'squad']:
            (instructions / (name + '.md')).write_text('instructions')
        original = self.api.request
        saved = {}
        def request(path, data=None):
            if path.startswith('agents/'):
                return original(path, data)
            if data:
                saved[path] = data
            if path.endswith('/resources'):
                return {'resources': [{'id': 'resource', **saved['projects/project/resources/resource']}]}
            return saved[path]
        config = {**self.deployment, 'ops_path': '/ops', 'squad_id': 'squad', 'project_id': 'project',
                  'resource_id': 'resource', 'repository': 'repo',
                  'max_concurrent_tasks': {'generator': 1, 'worker': 2, 'reviewer': 2}}
        with patch.object(self.api, 'request', side_effect=request):
            configure.configure(self.api, config, self.root, 'keep-ref')
        self.assertEqual({r: a['max_concurrent_tasks'] for r, a in self.api.agents.items()}, config['max_concurrent_tasks'])

    def test_unrelated_secret_or_external_link_never_shared(self):
        self.migrate()
        target = self.root / 'provider-auth/auth.json'
        secret = target.parent / 'operator.json'
        common.write_private(secret, {'admin': 'private'})
        with self.assertRaisesRegex(RuntimeError, 'NOT_DEDICATED'):
            shared_auth.auth_mode(target, ['shared-oauth-v1'])
        secret.unlink()
        (target.parent / 'auth.json.lock').unlink()
        (target.parent / 'auth.json.lock').symlink_to(self.operator)
        with self.assertRaisesRegex(RuntimeError, 'AUTH_PATH_INVALID'):
            shared_auth.auth_mode(target, ['shared-oauth-v1'])

    def test_legacy_and_new_process_share_the_same_flock(self):
        self.migrate()
        target = self.root / 'provider-auth/auth.json'
        legacy = shared_auth.auth_lock(target)
        try:
            with self.assertRaisesRegex(RuntimeError, 'AUTH_BUSY'):
                shared_auth.auth_lock(target, nonblocking=True)
            code = "import fcntl,sys; f=open(sys.argv[1], 'r+'); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)"
            child = subprocess.run([sys.executable, '-c', code, str(target) + '.lock'], capture_output=True)
            self.assertNotEqual(child.returncode, 0)
            self.assertIn(b'BlockingIOError', child.stderr)
        finally:
            os.close(legacy)
        self.assertEqual(subprocess.run([sys.executable, '-c', code, str(target) + '.lock'], capture_output=True).returncode, 0)

    def test_private_atomic_temps_do_not_prevent_parallel_refresh_or_migration_retry(self):
        self.migrate()
        directory = self.root / 'provider-auth'
        for name in ['auth.json.tmp-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                     'refresh-pending.json.tmp-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'marker.json.tmp-123']:
            common.write_private(directory / name, {'test': True})
        self.assertEqual(shared_auth.shared_directory(directory / 'auth.json'), directory)
        self.migrate()

    def test_launcher_error_releases_legacy_flock(self):
        self.migrate()
        target = self.root / 'provider-auth/auth.json'
        with self.assertRaises(FileNotFoundError):
            linux.run(['fake'], attempt=self.root, payload=self.root, cwd=self.root,
                      profile=self.root / 'missing-profile', auth=target)
        os.close(shared_auth.auth_lock(target, nonblocking=True))

    def test_launcher_mounts_only_dedicated_auth_and_legacy_keeps_lock(self):
        self.migrate()
        target = self.root / 'provider-auth/auth.json'
        attempt = self.root / 'attempt'; attempt.mkdir()
        profile = attempt / 'profile'; (profile / 'data').mkdir(parents=True)
        observed = []
        class Process:
            returncode = 0
            def __init__(self, argv, **kwargs):
                observed.append((argv, kwargs))
            def communicate(self, *args, **kwargs): pass
            def poll(self): return 0
        with patch.object(linux.subprocess, 'Popen', Process):
            linux.run(['fake-cli'], attempt=attempt, payload=self.root / 'payload', cwd=attempt,
                      profile=profile, auth=target, capabilities=['shared-oauth-v1'])
            linux.run(['fake-cli'], attempt=attempt, payload=self.root / 'payload', cwd=attempt,
                      profile=profile, auth=target)
        shared, legacy = observed
        self.assertIn('/run/loginom-auth', shared[0])
        self.assertIn('LOGINOM_AI_AGENT_SHARED_AUTH_DIR', shared[0])
        self.assertEqual(shared[1]['pass_fds'], ())
        self.assertNotIn('LOGINOM_AI_AGENT_SHARED_AUTH_DIR', legacy[0])
        self.assertEqual(len(legacy[1]['pass_fds']), 1)
        self.assertNotIn(str(self.operator), shared[0])

    def test_uncertain_refresh_prevents_legacy_bypass(self):
        self.migrate()
        target = self.root / 'provider-auth/auth.json'
        common.write_private(target.parent / 'refresh-pending.json', {'provider': 'test', 'hash': 'test'})
        with patch.object(linux.subprocess, 'Popen') as process:
            with self.assertRaisesRegex(RuntimeError, 'OAUTH_REFRESH_RECOVERY_REQUIRED'):
                linux.run(['fake-cli'], attempt=self.root, payload=self.root, cwd=self.root,
                          profile=self.root, auth=target)
            process.assert_not_called()
        os.close(shared_auth.auth_lock(target, nonblocking=True))


if __name__ == '__main__':
    unittest.main()
