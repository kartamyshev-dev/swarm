import importlib.util
from pathlib import Path
import sys
import unittest

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
        result = {'stage': 'stage0', 'provider_model': 'NOT_RUN'}
        self.assertEqual(len(publisher.publication_secrets(self.config, result)), 2)
        for item in [{}, {'stage': 'stage0'}, {'stage': 'stage1', 'provider_model': 'NOT_RUN'}]:
            with self.assertRaisesRegex(RuntimeError, 'EVIDENCE_PROVIDER_BOUNDARY_REQUIRED'):
                publisher.publication_secrets(self.config, item)

    def test_secret_and_bearer_patterns_are_rejected(self):
        for body in [b'loginom-password', b'Authorization: Bearer testcredential', b'sk-proj-abcdefghijklmnopqrst']:
            with self.assertRaises(RuntimeError):
                publisher.check_publication(body, ['loginom-password'])
        publisher.check_publication(b'{"status":"PASS","access_token":"[redacted]"}', ['loginom-password'])


if __name__ == '__main__':
    unittest.main()
