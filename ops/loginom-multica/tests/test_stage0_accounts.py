import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import common

spec = importlib.util.spec_from_file_location('stage0_accounts', SCRIPTS / 'provision-accounts.py')
provision = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provision)


class Stage0AccountTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.issue = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        self.operator = self.root / 'operator.json'
        self.cards = self.root / 'cards'
        self.settings = {'workspace_id': 'workspace', 'agents': {'worker': 'worker', 'reviewer': 'reviewer'},
                         'url': 'https://test.invalid', 'api_key': 'private'}
        common.write_private(self.operator, self.settings)

    def pool(self):
        return ['/missing/account-' + str(index) + '/auth.json' for index in range(8)]

    def configs(self, paths):
        return [common.read_private(path) for path in paths]

    def test_stage0_creates_pair_without_oauth_catalog_or_model_fields(self):
        common.write_private(self.operator, {**self.settings, 'provider_auth_files': {'unreadable': '/missing/auth.json'},
                                            'provider_auth_file': '/missing/legacy-auth.json'})
        paths = provision.allocate(self.issue, self.operator, self.cards, 'stage0')
        configs = self.configs(paths)
        self.assertEqual([config['role'] for config in configs], ['worker', 'reviewer'])
        self.assertEqual(len({config['loginom']['username'] for config in configs}), 2)
        for config, path in zip(configs, paths):
            self.assertEqual(config['stage'], 'stage0')
            self.assertEqual(config['account_state'], 'planned')
            self.assertFalse({'provider_auth_file', 'provider_auth_files', 'model', 'variant', 'model_cache_file'} & config.keys())
            self.assertEqual(path.stat().st_mode & 0o077, 0)

    def test_stage0_cli_requires_explicit_stage_and_works_without_catalog(self):
        command = [sys.executable, str(SCRIPTS / 'provision-accounts.py'), '--issue', self.issue,
                   '--operator', str(self.operator), '--directory', str(self.cards), '--allocate-only']
        full = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(full.returncode, 1)
        self.assertIn('EIGHT_PROVIDER_AUTH_FILES_REQUIRED', full.stderr)
        self.assertFalse(self.cards.exists())
        stage0 = subprocess.run(command + ['--stage', 'stage0'], capture_output=True, text=True)
        self.assertEqual(stage0.returncode, 0, stage0.stderr)
        output = json.loads(stage0.stdout)
        self.assertEqual(output['stage'], 'stage0')
        self.assertFalse(output['verified'])
        self.assertTrue(all(config['stage'] == 'stage0' for config in self.configs(map(Path, output['configs']))))

    def test_full_default_still_requires_exactly_eight_auth_paths(self):
        for pool in [None, self.pool()[:7], self.pool() + ['/missing/ninth.json'], [self.pool()[0]] * 8]:
            with self.subTest(pool=pool):
                common.write_private(self.operator, {**self.settings, 'provider_auth_files': pool})
                with self.assertRaisesRegex(RuntimeError, 'EIGHT_PROVIDER_AUTH_FILES_REQUIRED'):
                    provision.allocate(self.issue, self.operator, self.cards)
        self.assertFalse(self.cards.exists())

    def test_stage0_retry_preserves_identity_secrets_and_uncertain_states(self):
        paths = provision.allocate(self.issue, self.operator, self.cards, 'stage0')
        for path, state in zip(paths, ['creating', 'ready']):
            common.write_private(path, {**common.read_private(path), 'account_state': state})
        saved = [path.read_bytes() for path in paths]
        common.write_private(self.operator, {**self.settings, 'url': 'https://other.invalid', 'api_key': 'changed'})
        self.assertEqual(provision.allocate(self.issue, self.operator, self.cards, 'stage0'), paths)
        self.assertEqual([path.read_bytes() for path in paths], saved)

    def test_retry_rejects_new_operator_file_without_rebinding(self):
        paths = provision.allocate(self.issue, self.operator, self.cards, 'stage0')
        saved = [path.read_bytes() for path in paths]
        other = self.root / 'other-operator.json'
        common.write_private(other, self.settings)
        with self.assertRaisesRegex(RuntimeError, 'OPERATOR_BINDING_MISMATCH'):
            provision.allocate(self.issue, other, self.cards, 'stage0')
        self.assertEqual([path.read_bytes() for path in paths], saved)

    def test_retry_rejects_changed_workspace_and_agent_bindings(self):
        paths = provision.allocate(self.issue, self.operator, self.cards, 'stage0')
        saved = [path.read_bytes() for path in paths]
        for settings, code in [({**self.settings, 'workspace_id': 'other'}, 'WORKSPACE_MISMATCH'),
                               ({**self.settings, 'agents': {'worker': 'worker', 'reviewer': 'other'}}, 'ACCOUNT_BINDING_MISMATCH')]:
            with self.subTest(code=code):
                common.write_private(self.operator, settings)
                with self.assertRaisesRegex(RuntimeError, code):
                    provision.allocate(self.issue, self.operator, self.cards, 'stage0')
                self.assertEqual([path.read_bytes() for path in paths], saved)

    def test_default_full_retry_cannot_enable_model_for_stage0(self):
        paths = provision.allocate(self.issue, self.operator, self.cards, 'stage0')
        saved = [path.read_bytes() for path in paths]
        common.write_private(self.operator, {**self.settings, 'provider_auth_files': self.pool()})
        with self.assertRaisesRegex(RuntimeError, 'ACCOUNT_STAGE_MISMATCH'):
            provision.allocate(self.issue, self.operator, self.cards)
        self.assertEqual([path.read_bytes() for path in paths], saved)

    def test_explicit_stage0_cannot_reclassify_full_or_legacy_pair(self):
        common.write_private(self.operator, {**self.settings, 'provider_auth_files': self.pool()})
        paths = provision.allocate(self.issue, self.operator, self.cards)
        for legacy in [False, True]:
            with self.subTest(legacy=legacy):
                if legacy:
                    for path in paths:
                        config = common.read_private(path)
                        config.pop('stage')
                        common.write_private(path, config)
                saved = [path.read_bytes() for path in paths]
                with self.assertRaisesRegex(RuntimeError, 'ACCOUNT_STAGE_MISMATCH'):
                    provision.allocate(self.issue, self.operator, self.cards, 'stage0')
                self.assertEqual([path.read_bytes() for path in paths], saved)

    def test_both_roles_are_validated_before_pool_refresh(self):
        common.write_private(self.operator, {**self.settings, 'provider_auth_files': self.pool()})
        paths = provision.allocate(self.issue, self.operator, self.cards)
        common.write_private(paths[1], {**common.read_private(paths[1]), 'stage': 'stage0'})
        saved = [path.read_bytes() for path in paths]
        common.write_private(self.operator, {**self.settings, 'provider_auth_files': ['/new/' + str(i) + '/auth.json' for i in range(8)]})
        with self.assertRaisesRegex(RuntimeError, 'ACCOUNT_STAGE_MISMATCH'):
            provision.allocate(self.issue, self.operator, self.cards)
        self.assertEqual([path.read_bytes() for path in paths], saved)

    def test_stage0_retry_rejects_unknown_state_and_auth_contamination(self):
        paths = provision.allocate(self.issue, self.operator, self.cards, 'stage0')
        original = common.read_private(paths[1])
        for patch, code in [({'account_state': 'unknown'}, 'ACCOUNT_STATE_INVALID'),
                            ({'provider_auth_file': '/missing/auth.json'}, 'STAGE0_PROVIDER_AUTH_FORBIDDEN'),
                            ({'provider_auth_files': self.pool()}, 'STAGE0_PROVIDER_AUTH_FORBIDDEN')]:
            with self.subTest(code=code, patch=patch):
                common.write_private(paths[1], {**original, **patch})
                saved = [path.read_bytes() for path in paths]
                with self.assertRaisesRegex(RuntimeError, code):
                    provision.allocate(self.issue, self.operator, self.cards, 'stage0')
                self.assertEqual([path.read_bytes() for path in paths], saved)


if __name__ == '__main__':
    unittest.main()
