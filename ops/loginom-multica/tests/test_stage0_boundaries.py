import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import accept
import common
spec = importlib.util.spec_from_file_location('stage0_publisher', SCRIPTS / 'publish-evidence.py')
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


class Stage0BoundaryTests(unittest.TestCase):
    def test_native_publication_never_reads_oauth_and_reconciles_existing_receipt(self):
        for credentials in ['legacy', 'pool', 'none']:
            with self.subTest(credentials=credentials), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                attempt = root / 'attempts/native'
                evidence = attempt / 'evidence'; evidence.mkdir(parents=True)
                owner = {'workspace_id': 'workspace', 'issue_id': 'issue', 'agent_id': 'worker'}
                auth = root / 'unreadable-auth.json'
                common.write_private(auth, {'openai': {'refresh': 'must-never-be-read'}})
                auth.chmod(0)
                config = {'stage': 'stage0', **owner, 'loginom': {'password': 'loginom-password', 'api_key': 'loginom-key'}}
                if credentials == 'legacy':
                    config['provider_auth_file'] = str(auth)
                if credentials == 'pool':
                    config['provider_auth_files'] = [str(auth)]
                config_path = root / 'role.json'; common.write_private(config_path, config)
                result = {**owner, 'status': 'PASS', 'source_sha': 'commit', 'stage': 'stage0', 'provider_model': 'NOT_RUN'}
                common.write_private(evidence / 'result.json', result)
                (evidence / 'events.jsonl').write_text('{"type":"native_research"}\n')
                content = 'Native Stage 0 research. Model and product CLI: NOT_RUN.'
                content_path = attempt / 'comment.md'; content_path.write_text(content)
                bodies = {path.name: path.read_bytes() for path in evidence.iterdir()}
                digests = {name: hashlib.sha256(body).hexdigest() for name, body in bodies.items()}
                marker = hashlib.sha256(json.dumps([digests, content], sort_keys=True).encode()).hexdigest()
                comment = {'id': 'comment', 'issue_id': 'issue', 'content': content + '\n\nEvidence receipt: ' + marker,
                           'attachments': [{'filename': name, 'id': name, 'comment_id': 'comment'} for name in bodies]}
                requests = []
                class API:
                    def request(self, path, data=None, **kwargs):
                        requests.append((path, data))
                        if path == 'issues/issue': return {'status': 'in_progress'}
                        if path == 'issues/issue/comments?full=true': return [comment]
                        if path.startswith('attachments/'): return bodies[path.split('/')[1]]
                        raise AssertionError('unexpected API request')
                original = publisher.read_private
                def read(path):
                    if Path(path) == auth:
                        raise AssertionError('Stage 0 attempted to read OAuth')
                    return original(path)
                args = ['publish-evidence.py', '--worktree', str(root), '--config', str(config_path), '--attempt', str(attempt), '--content-file', str(content_path)]
                output = StringIO()
                with patch.object(sys, 'argv', args), patch.object(publisher, 'managed_root', return_value=(root, owner)), \
                     patch.object(publisher, 'read_private', side_effect=read), patch.object(publisher, 'API', return_value=API()), \
                     patch.object(publisher.subprocess, 'run', side_effect=AssertionError('existing receipt must be read back')) as post, redirect_stdout(output):
                    publisher.main()
                post.assert_not_called()
                self.assertTrue(all(data is None for _, data in requests))
                receipt = common.read_private(attempt / 'publication-receipt.json')
                self.assertTrue(receipt['verified'])
                self.assertEqual(receipt['stage'], 'stage0')
                self.assertEqual(receipt['provider_model'], 'NOT_RUN')
                self.assertEqual(receipt['files'], digests)
                self.assertEqual(json.loads(output.getvalue())['provider_model'], 'NOT_RUN')

    def test_relabelled_model_result_is_rejected_before_provider_reads_or_external_posts(self):
        for model in [{'provider_auth_file': '/unreadable/auth.json', 'redaction_complete': True},
                      {'model': 'openai/test', 'variant': 'high', 'cli_exit': 0,
                       'redaction_complete': True, 'redacted_files': {'events.jsonl': 'digest'}}]:
            with self.subTest(model=model), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                attempt = root / 'attempts/relabelled'; evidence = attempt / 'evidence'
                evidence.mkdir(parents=True)
                owner = {'workspace_id': 'workspace', 'issue_id': 'issue', 'agent_id': 'worker'}
                config = root / 'role.json'
                common.write_private(config, {'stage': 'stage0', **owner, 'provider_auth_file': '/unreadable/auth.json',
                    'loginom': {'password': 'loginom-password', 'api_key': 'loginom-key'}})
                common.write_private(evidence / 'result.json', {**owner, 'status': 'PASS', 'source_sha': 'commit',
                    'stage': 'stage0', 'provider_model': 'NOT_RUN', **model})
                (evidence / 'events.jsonl').write_text('{"text":"unsanitized old provider output"}\n')
                content = attempt / 'comment.md'; content.write_text('Relabelled model acceptance')
                original = publisher.read_private
                def read(path):
                    if str(path) == '/unreadable/auth.json':
                        raise AssertionError('Rejected evidence must not read OAuth')
                    return original(path)
                args = ['publish-evidence.py', '--worktree', str(root), '--config', str(config), '--attempt', str(attempt), '--content-file', str(content)]
                with patch.object(sys, 'argv', args), patch.object(publisher, 'managed_root', return_value=(root, owner)), \
                     patch.object(publisher, 'read_private', side_effect=read), \
                     patch.object(publisher, 'API', side_effect=AssertionError('Rejected evidence must not call the API')) as api, \
                     patch.object(publisher.subprocess, 'run', side_effect=AssertionError('Rejected evidence must not post')) as post:
                    with self.assertRaisesRegex(RuntimeError, 'STAGE0_.*_FORBIDDEN'):
                        publisher.main()
                api.assert_not_called(); post.assert_not_called()
                self.assertFalse((attempt / 'publication-receipt.json').exists())
                self.assertFalse((attempt / 'publication/events.jsonl').exists())

    def test_stage0_role_rejects_model_acceptance_before_any_attempt_or_auth_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config = root / 'role.json'
            common.write_private(config, {'stage': 'stage0', 'provider_auth_file': str(root / 'absent-auth.json')})
            out = root / 'attempts/forbidden'
            args = ['accept.py', '--worktree', str(root), '--node', 'test', '--config', str(config), '--cli', str(root / 'missing-cli'), '--out', str(out)]
            with patch.object(sys, 'argv', args), patch.object(accept, 'managed_root', side_effect=AssertionError('preflight must precede workspace writes')) as managed, \
                 patch.object(accept, 'acquire', side_effect=AssertionError('Stage 0 must not acquire OAuth')) as acquire, \
                 patch.object(accept, 'run', side_effect=AssertionError('Stage 0 must not run a model')) as run:
                with self.assertRaisesRegex(RuntimeError, 'STAGE0_MODEL_ACCEPTANCE_FORBIDDEN'):
                    accept.main()
            managed.assert_not_called(); acquire.assert_not_called(); run.assert_not_called()
            self.assertFalse(out.exists())


if __name__ == '__main__':
    unittest.main()
