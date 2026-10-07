import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('publisher_pool', SCRIPTS / 'publish-evidence.py')
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


class PublicationPoolTests(unittest.TestCase):
    def setUp(self):
        self.config = {'provider_auth_files': ['/abs/slot-' + str(n) for n in range(8)],
                       'loginom': {'password': 'loginom-password', 'api_key': 'loginom-key'}}

    def test_publication_does_not_read_rotating_provider_files(self):
        result = {'provider_auth_file': self.config['provider_auth_files'][0], 'redaction_complete': True}
        self.assertEqual(publisher.publication_secrets(self.config, result), ['loginom-password', 'loginom-key'])

    def test_foreign_auth_and_missing_redaction_refuse_publication(self):
        for result in [{'provider_auth_file': '/other/auth', 'redaction_complete': True},
                       {'provider_auth_file': self.config['provider_auth_files'][0]}]:
            with self.assertRaisesRegex(RuntimeError, 'PROVIDER_REDACTION_UNCONFIRMED'):
                publisher.publication_secrets(self.config, result)

    def test_native_stage0_requires_explicit_model_not_run(self):
        config = {**self.config, 'stage': 'stage0'}
        result = {'stage': 'stage0', 'provider_model': 'NOT_RUN'}
        self.assertEqual(len(publisher.publication_secrets(config, result)), 2)
        for item in [{}, {'stage': 'stage0'}, {'stage': 'stage1', 'provider_model': 'NOT_RUN'}]:
            with self.assertRaisesRegex(RuntimeError, 'STAGE0_MODEL_BOUNDARY_REQUIRED'):
                publisher.publication_secrets(config, item)
        with self.assertRaisesRegex(RuntimeError, 'EVIDENCE_STAGE_BOUNDARY_MISMATCH'):
            publisher.publication_secrets(self.config, result)

    def test_full_evidence_cannot_bypass_provider_boundary(self):
        with self.assertRaisesRegex(RuntimeError, 'PROVIDER_AUTH_REQUIRED'):
            publisher.publication_secrets({'loginom': self.config['loginom']}, {})
        with self.assertRaisesRegex(RuntimeError, 'EVIDENCE_PROVIDER_BOUNDARY_REQUIRED'):
            publisher.publication_secrets(self.config, {})
        legacy = {'provider_auth_file': '/unreadable/auth.json', 'loginom': self.config['loginom']}
        with patch.object(publisher, 'read_private', side_effect=PermissionError('unreadable')) as read:
            with self.assertRaises(PermissionError):
                publisher.publication_secrets(legacy, {})
        read.assert_called_once_with(Path('/unreadable/auth.json'))

    def test_stage0_never_accepts_selected_oauth_or_model_receipt_markers(self):
        config = {**self.config, 'stage': 'stage0', 'provider_auth_file': '/unreadable/auth.json'}
        native = {'stage': 'stage0', 'provider_model': 'NOT_RUN'}
        markers = [{'provider_auth_file': '/unreadable/auth.json'}, {'provider_auth_file': None},
                   {'provider_auth_files': []}, {'model': 'openai/test'}, {'variant': 'high'},
                   {'cli_exit': 0}, {'redaction_complete': True}, {'redacted_files': {'events.jsonl': 'digest'}}]
        with patch.object(publisher, 'read_private', side_effect=AssertionError('Stage 0 must not read OAuth')) as read:
            for marker in markers:
                with self.subTest(marker=marker), self.assertRaisesRegex(RuntimeError, 'STAGE0_.*_FORBIDDEN'):
                    publisher.publication_secrets(config, {**native, **marker})
            self.assertEqual(len(publisher.publication_secrets(config, {**native, 'model': 'NOT_RUN', 'variant': 'NOT_RUN', 'cli_exit': 'NOT_RUN'})), 2)
        read.assert_not_called()

    def test_secret_and_bearer_patterns_are_rejected(self):
        for body in [b'loginom-password', b'Authorization: Bearer testcredential', b'sk-proj-abcdefghijklmnopqrst']:
            with self.assertRaises(RuntimeError):
                publisher.check_publication(body, ['loginom-password'])
        publisher.check_publication(b'{"status":"PASS","access_token":"[redacted]"}', ['loginom-password'])


if __name__ == '__main__':
    unittest.main()
