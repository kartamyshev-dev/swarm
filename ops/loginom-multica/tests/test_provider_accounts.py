import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import common
import provider_pool
import shared_auth

spec = importlib.util.spec_from_file_location('prepare_provider_accounts', SCRIPTS / 'prepare-provider-accounts.py')
onboarding = importlib.util.module_from_spec(spec)
spec.loader.exec_module(onboarding)


class API:
    def __init__(self):
        self.tasks = [{'id': 'service-task', 'workspace_id': 'workspace', 'issue_id': 'service',
                       'agent_id': 'worker', 'status': 'running'}]

    def request(self, path):
        if path.startswith('agents/'):
            return {'id': path.removeprefix('agents/')}
        if path == 'agent-task-snapshot':
            return self.tasks
        if path == 'issues/service':
            return {'id': 'service', 'workspace_id': 'workspace', 'assignee_type': 'agent', 'assignee_id': 'worker'}
        raise AssertionError(path)


class ProviderAccountsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.profiles = self.root / 'provider-accounts'
        self.api = API()
        self.deployment = {'workspace_id': 'workspace', 'maintenance_issue_id': 'service',
                           'agents': {role: role for role in ['generator', 'worker', 'reviewer']}}
        self.environment = patch.dict(os.environ, {'MULTICA_WORKSPACE_ID': 'workspace',
                                                   'MULTICA_AGENT_ID': 'worker', 'MULTICA_TASK_ID': 'service-task'})
        self.environment.start()
        self.payload = self.root / 'payload'
        (self.payload / 'bin').mkdir(parents=True)
        self.cli = self.payload / 'bin/loginom-ai-agent-cli'
        self.install_cli('''#!/usr/bin/env python3
import base64, json, os
from pathlib import Path
profile = Path(os.environ['LOGINOM_AI_AGENT_CLI_PROFILE'])
slot = int(profile.parent.name)
assert os.environ['LOGINOM_AI_AGENT_CHANNEL'] == 'dev'
assert 'LOGINOM_AI_AGENT_AUTH_CONTENT' not in os.environ
assert 'OPENAI_API_KEY' not in os.environ
assert os.sys.argv[1:] == ['providers', 'login', '--provider', 'openai', '--method', 'ChatGPT Pro/Plus (headless)']
print('Go to: https://auth.openai.com/codex/device', flush=True)
print('Enter code: FAKE-1234', flush=True)
print('secret access-DO-NOT-PRINT refresh-DO-NOT-PRINT', flush=True)
claim = base64.urlsafe_b64encode(json.dumps({'https://api.openai.com/auth': {'chatgpt_user_id': 'user-' + str(slot)}}).encode()).decode().rstrip('=')
(profile / 'data/auth.json').write_text(json.dumps({'openai': {'type': 'oauth', 'access': 'header.' + claim + '.signature', 'refresh': 'refresh-' + str(slot), 'expires': 1234}}))
(profile / 'data/model.db').write_text('private model state')
(profile / 'cache/private.log').write_text('private login diagnostics')
(profile / 'config/private.json').write_text('{}')
print('Login successful', flush=True)
''')

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def install_cli(self, content):
        self.cli.write_text(content)
        self.cli.chmod(0o755)
        (self.payload / 'cli-manifest.json').write_text(json.dumps({
            'format': 'loginom-cli-artifact-v1',
            'metadata': {'channel': 'dev', 'sourceDirty': False, 'platform': sys.platform,
                         'arch': {'x86_64': 'x64', 'AMD64': 'x64', 'aarch64': 'arm64', 'arm64': 'arm64'}[platform.machine()],
                         'sourceCommit': 'a' * 40, 'sourceTreeSha256': 'b' * 64},
            'files': [{'path': 'bin/loginom-ai-agent-cli', 'mode': 0o755,
                       'sha256': hashlib.sha256(self.cli.read_bytes()).hexdigest()}]}))

    def prepare(self):
        return onboarding.prepare(self.api, self.deployment, self.profiles, self.cli)

    def authorize_all(self):
        self.prepare()
        with contextlib.redirect_stdout(io.StringIO()):
            for slot in range(1, 9):
                onboarding.login(self.api, self.deployment, self.profiles, self.cli, slot)

    def test_profiles_are_private_persistent_and_not_old_auth_copies(self):
        old = self.root / 'old/auth.json'
        common.write_private(old, {'openai': {'refresh': 'old-secret'}})
        result = self.prepare()
        self.assertEqual(result['profiles'], 8)
        paths = list(map(Path, result['provider_auth_files']))
        self.assertEqual(len(set(path.stat().st_ino for path in paths)), 8)
        for auth in paths:
            self.assertEqual(auth.stat().st_mode & 0o777, 0o600)
            self.assertEqual(auth.read_text().strip(), '{}')
            self.assertEqual(auth.parent.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(common.read_private(auth.parent.parent / 'cli-profile.json'),
                             {'format': 'loginom-cli', 'version': 1, 'channel': 'dev'})
        self.assertIn('old-secret', old.read_text())
        work = self.root / 'multica_workspaces/card'
        work.mkdir(parents=True)
        (work / 'attempt').write_text('obsolete')
        import shutil
        shutil.rmtree(work.parent)
        self.assertTrue(all(path.exists() for path in paths))

    def test_login_uses_existing_cli_flags_hides_secrets_and_retains_only_auth(self):
        paths = list(map(Path, self.prepare()['provider_auth_files']))
        inode = paths[0].stat().st_ino
        output = io.StringIO()
        with patch.dict(os.environ, {'LOGINOM_AI_AGENT_AUTH_CONTENT': 'secret', 'OPENAI_API_KEY': 'secret'}):
            with contextlib.redirect_stdout(output):
                result = onboarding.login(self.api, self.deployment, self.profiles, self.cli, 1)
        self.assertEqual(result['state'], 'authorized')
        self.assertEqual(paths[0].stat().st_ino, inode)
        self.assertIn('FAKE-1234', output.getvalue())
        self.assertIn('https://auth.openai.com/codex/device', output.getvalue())
        self.assertNotIn('DO-NOT-PRINT', output.getvalue())
        self.assertFalse(provider_pool.health_path(paths[0]).exists())
        self.assertEqual({entry.name for entry in paths[0].parent.iterdir()}, {'auth.json', 'auth.json.lock'})
        self.assertFalse((paths[0].parent.parent / 'cache/private.log').exists())

    def test_activation_requires_all_eight_and_preserves_other_configuration(self):
        operator = self.root / 'operator.json'
        original = {'workspace_id': 'workspace', 'provider_auth_file': '/unused/auth.json',
                    'model': 'unchanged', 'admin_password': 'private-test-password'}
        common.write_private(operator, original)
        cards = self.root / 'cards'
        cards.mkdir(mode=0o700)
        role = cards / 'card/worker.json'
        common.write_private(role, {'workspace_id': 'workspace', 'operator_file': str(operator), 'issue_id': 'card',
                                   'agent_id': 'worker', 'role': 'worker', 'provider_auth_file': '/unused/auth.json',
                                   'loginom': {'username': 'keep-user'}})
        self.prepare()
        with self.assertRaisesRegex(RuntimeError, 'PROVIDER_AUTH_INVALID'):
            onboarding.activate(self.api, self.deployment, self.profiles, operator, cards)
        self.assertEqual(common.read_private(operator), original)
        self.authorize_all()
        self.assertEqual(onboarding.check(self.profiles)['state'], 'ready')
        result = onboarding.activate(self.api, self.deployment, self.profiles, operator, cards)
        self.assertEqual(result['configs'], 2)
        self.assertEqual(result['readback'], 'verified')
        configured = common.read_private(operator)
        self.assertNotIn('provider_auth_file', configured)
        self.assertEqual(configured['model'], original['model'])
        self.assertEqual(configured['admin_password'], original['admin_password'])
        self.assertEqual(len(configured['provider_auth_files']), 8)
        self.assertEqual(common.read_private(role)['loginom'], {'username': 'keep-user'})
        self.assertEqual(common.read_private(role)['provider_auth_files'], configured['provider_auth_files'])

    def test_other_active_task_and_missing_service_context_block_mutations(self):
        self.api.tasks.append({'id': 'other', 'status': 'queued'})
        with self.assertRaisesRegex(RuntimeError, 'WORKSPACE_NOT_IDLE'):
            self.prepare()
        self.assertFalse(self.profiles.exists())
        self.api.tasks.pop()
        with patch.dict(os.environ, {'MULTICA_TASK_ID': ''}):
            with self.assertRaisesRegex(RuntimeError, 'NATIVE_MAINTENANCE_REQUIRED'):
                self.prepare()
        self.api.tasks.clear()
        with self.assertRaisesRegex(RuntimeError, 'NATIVE_MAINTENANCE_REQUIRED'):
            self.prepare()

    def test_managed_or_linked_profile_root_and_modified_cli_are_rejected(self):
        managed = self.root / 'managed'
        managed.mkdir()
        (managed / '.managed_env.json').write_text('{}')
        with self.assertRaisesRegex(RuntimeError, 'PROVIDER_PROFILES_INSIDE_MANAGED_CHECKOUT'):
            onboarding.prepare(self.api, self.deployment, managed / 'profiles', self.cli)
        linked = self.root / 'linked'
        linked.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'PROVIDER_PROFILE_ROOT_INVALID'):
            onboarding.prepare(self.api, self.deployment, linked / 'profiles', self.cli)
        self.cli.write_text('changed')
        with self.assertRaisesRegex(RuntimeError, 'CLI_INTEGRITY_INVALID'):
            self.prepare()

    def test_busy_lock_timeout_and_failed_reauthorization_do_not_recover(self):
        auth = Path(self.prepare()['provider_auth_files'][0])
        fd = shared_auth.auth_lock(auth)
        try:
            with self.assertRaisesRegex(RuntimeError, 'AUTH_BUSY'):
                onboarding.login(self.api, self.deployment, self.profiles, self.cli, 1)
        finally:
            os.close(fd)
        self.install_cli('#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n')
        with self.assertRaisesRegex(RuntimeError, 'PROVIDER_LOGIN_TIMED_OUT'):
            onboarding.login(self.api, self.deployment, self.profiles, self.cli, 1, timeout=0.1)
        self.assertEqual(common.read_private(provider_pool.health_path(auth))['state'], 'quarantined')
        self.install_cli('#!/usr/bin/env python3\nraise SystemExit(2)\n')
        with self.assertRaisesRegex(RuntimeError, 'PROVIDER_LOGIN_FAILED'):
            onboarding.login(self.api, self.deployment, self.profiles, self.cli, 1)
        self.assertTrue(provider_pool.health_path(auth).exists())

    def test_readiness_rejects_duplicate_tokens_and_quarantined_auth(self):
        self.authorize_all()
        paths = onboarding.catalog(self.profiles)
        common.write_private(provider_pool.health_path(paths[0]), {'state': 'quarantined', 'reason': 'test'})
        with self.assertRaisesRegex(RuntimeError, 'PROVIDER_AUTH_RECOVERY_REQUIRED'):
            onboarding.check(self.profiles)
        provider_pool.recover(paths[0])
        common.write_private(paths[1], common.read_private(paths[0]))
        with self.assertRaisesRegex(RuntimeError, 'PROVIDER_AUTH_DUPLICATE'):
            onboarding.check(self.profiles)


if __name__ == '__main__':
    unittest.main()
