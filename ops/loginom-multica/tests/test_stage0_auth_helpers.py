import base64
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import common
import shared_auth


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), SCRIPTS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


onboarding = load('prepare-provider-accounts')
migration = load('prepare-parallel-auth')


class API:
    def request(self, path, data=None):
        if data is not None:
            raise AssertionError('API mutation is forbidden')
        if path.startswith('agents/'):
            return {'id': path.removeprefix('agents/')}
        if path == 'agent-task-snapshot':
            return [{'id': 'service-task', 'workspace_id': 'workspace', 'issue_id': 'service',
                     'agent_id': 'worker', 'status': 'running'}]
        if path == 'issues/service':
            return {'id': 'service', 'workspace_id': 'workspace', 'assignee_type': 'agent', 'assignee_id': 'worker'}
        raise AssertionError(path)


class Stage0AuthHelperTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.api = API()
        self.deployment = {'workspace_id': 'workspace', 'maintenance_issue_id': 'service',
                           'agents': {role: role for role in ['generator', 'worker', 'reviewer']}}
        environment = patch.dict(os.environ, {'MULTICA_WORKSPACE_ID': 'workspace',
                                               'MULTICA_AGENT_ID': 'worker', 'MULTICA_TASK_ID': 'service-task'})
        environment.start()
        self.addCleanup(environment.stop)
        self.auth = self.root / 'auth.json'
        common.write_private(self.auth, {'provider': {'type': 'oauth', 'refresh': 'fake'}})
        descriptor = shared_auth.auth_lock(self.auth)
        os.close(descriptor)
        self.operator = self.root / 'operator.json'
        common.write_private(self.operator, {'workspace_id': 'workspace', 'provider_auth_file': str(self.auth),
                                            'model': 'keep-model', 'admin_password': 'private'})
        self.cards = self.root / 'cards'
        self.cards.mkdir(mode=0o700)
        self.full, self.legacy, self.stage0 = [], [], []
        # Stage 0 is scanned after full roles, making preflight-before-mutation observable.
        for issue, stage, paths in [('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'full', self.full),
                                    ('cccccccc-cccc-cccc-cccc-cccccccccccc', None, self.legacy),
                                    ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'stage0', self.stage0)]:
            for role in ['worker', 'reviewer']:
                path = self.cards / issue / (role + '.json')
                value = {'workspace_id': 'workspace', 'operator_file': str(self.operator), 'issue_id': issue,
                         'agent_id': role, 'role': role, 'loginom': {'username': issue + role, 'password': 'private'}}
                if stage is not None:
                    value['stage'] = stage
                if stage != 'stage0':
                    value.update({'provider_auth_file': str(self.auth), 'model': 'keep-model', 'variant': 'low'})
                common.write_private(path, value)
                if stage == 'stage0':
                    path.write_text(json.dumps(value, separators=(',', ':')) + '\n\n')
                paths.append(path)
        self.profiles = self.root / 'provider-accounts'
        for slot, path in enumerate(onboarding.catalog(self.profiles), 1):
            for directory in [self.profiles, path.parent.parent.parent, path.parent.parent, path.parent]:
                directory.mkdir(mode=0o700, exist_ok=True)
            claim = base64.urlsafe_b64encode(json.dumps({'https://api.openai.com/auth': {
                'chatgpt_user_id': 'fixture-user-' + str(slot)}}).encode()).decode().rstrip('=')
            common.write_private(path, {'openai': {'type': 'oauth', 'access': 'header.' + claim + '.signature',
                                                 'refresh': 'fixture-refresh-' + str(slot), 'expires': 0}})
            common.write_private(path.parent.parent / 'cli-profile.json', {'format': 'loginom-cli', 'version': 1, 'channel': 'dev'})

    def activate(self):
        return onboarding.activate(self.api, self.deployment, self.profiles, self.operator, self.cards)

    def migrate(self):
        return migration.migrate(self.api, self.deployment, self.operator, self.cards, source=self.auth)

    def snapshot(self):
        return {path: path.read_bytes() for path in [self.operator, *self.full, *self.legacy, *self.stage0]}

    def test_activation_updates_full_and_legacy_roles_but_stage0_is_byte_identical(self):
        saved = self.snapshot()
        result = self.activate()
        self.assertEqual(result['configs'], 5)
        pool = list(map(str, onboarding.catalog(self.profiles)))
        for path in [self.operator, *self.full, *self.legacy]:
            value = common.read_private(path)
            self.assertEqual(value['provider_auth_files'], pool)
            self.assertNotIn('provider_auth_file', value)
            original = json.loads(saved[path])
            original.pop('provider_auth_file')
            value.pop('provider_auth_files')
            self.assertEqual(value, original)
        self.assertEqual({path: path.read_bytes() for path in self.stage0}, {path: saved[path] for path in self.stage0})
        first = self.snapshot()
        self.assertEqual(self.activate(), result)
        self.assertEqual(self.snapshot(), first)

    def test_migration_preserves_stage0_bytes_token_identity_and_retry_journal(self):
        saved = self.snapshot()
        token_inode = self.auth.stat().st_ino
        lock_inode = Path(str(self.auth) + '.lock').stat().st_ino
        result = self.migrate()
        target = self.root / 'provider-auth/auth.json'
        self.assertEqual(result['configs'], 5)
        self.assertEqual(target.stat().st_ino, token_inode)
        self.assertEqual(Path(str(target) + '.lock').stat().st_ino, lock_inode)
        self.assertFalse(self.auth.exists())
        for path in [self.operator, *self.full, *self.legacy]:
            value = common.read_private(path)
            self.assertEqual(value['provider_auth_file'], str(target))
            original = json.loads(saved[path])
            original['provider_auth_file'] = str(target)
            self.assertEqual(value, original)
        self.assertEqual({path: path.read_bytes() for path in self.stage0}, {path: saved[path] for path in self.stage0})
        first = self.snapshot()
        self.assertEqual(self.migrate(), result)
        self.assertEqual(self.snapshot(), first)
        self.assertEqual(common.read_private(self.root / 'parallel-auth-migration.json')['state'], 'complete')

    def check_rejected_preflight(self, helper):
        cases = [({'operator_file': str(self.root / 'foreign.json')}, 'FOREIGN_ROLE_CONFIG'),
                 ({'workspace_id': 'foreign'}, 'FOREIGN_ROLE_CONFIG'),
                 ({'issue_id': 'foreign'}, 'FOREIGN_ROLE_CONFIG'),
                 ({'agent_id': 'foreign'}, 'FOREIGN_ROLE_CONFIG'),
                 ({'role': 'reviewer'}, 'FOREIGN_ROLE_CONFIG'),
                 ({'stage': 'stage1'}, 'ROLE_STAGE_INVALID'),
                 ({'stage': None}, 'ROLE_STAGE_INVALID')]
        original = self.stage0[0].read_bytes()
        token = self.auth.read_bytes()
        inode = self.auth.stat().st_ino
        lock_inode = Path(str(self.auth) + '.lock').stat().st_ino
        for changes, code in cases:
            with self.subTest(helper=helper.__name__, changes=changes):
                value = {**json.loads(original), **changes}
                common.write_private(self.stage0[0], value)
                before = self.snapshot()
                with self.assertRaisesRegex(RuntimeError, code):
                    helper()
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.auth.read_bytes(), token)
                self.assertEqual(self.auth.stat().st_ino, inode)
                self.assertEqual(Path(str(self.auth) + '.lock').stat().st_ino, lock_inode)
                self.assertFalse((self.root / 'provider-auth').exists())
                self.assertFalse((self.root / 'parallel-auth-migration.json').exists())
                self.stage0[0].write_bytes(original)

    def test_activation_rejects_foreign_or_unknown_stage_before_config_mutation(self):
        self.check_rejected_preflight(self.activate)

    def test_migration_rejects_foreign_or_unknown_stage_before_token_move(self):
        self.check_rejected_preflight(self.migrate)


if __name__ == '__main__':
    unittest.main()
