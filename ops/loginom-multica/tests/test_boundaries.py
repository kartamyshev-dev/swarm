import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from contextlib import redirect_stdout
from io import StringIO

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import common
import build
import accept


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), SCRIPTS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


provision = load('provision-accounts')
publisher = load('publish-evidence')


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self):
        self.temporary.cleanup()

    def test_paths_reject_escape_and_external_link(self):
        (self.root / 'outside').symlink_to('/tmp')
        for path in [self.root / '../foreign', self.root / 'outside/file', Path('/foreign')]:
            with self.assertRaises(RuntimeError):
                common.checked_path(path, self.root)

    def test_live_flock_blocks_second_builder_and_releases(self):
        lock = common.artifact_lock(self.root)
        try:
            with self.assertRaisesRegex(RuntimeError, 'ARTIFACT_BUSY'):
                common.artifact_lock(self.root)
        finally:
            os.close(lock)
        os.close(common.artifact_lock(self.root))

    def test_publication_rolls_back_on_rename_failure(self):
        current = self.root / 'current'; current.mkdir(); (current / 'old').write_text('old')
        temporary = self.root / 'stage'; temporary.mkdir()
        with self.assertRaises(FileNotFoundError):
            build.publish(temporary / 'missing', current, temporary)
        self.assertEqual((current / 'old').read_text(), 'old')

    def test_stalled_build_command_stops_and_releases_inherited_lock(self):
        lock = common.artifact_lock(self.root)
        with self.assertRaisesRegex(RuntimeError, 'BUILD_COMMAND_TIMED_OUT'):
            build.build_command([sys.executable, '-c', 'import time;time.sleep(300)'], self.root, lock, timeout=0.1)
        os.close(lock)
        os.close(common.artifact_lock(self.root))

    def test_verified_client_is_reused_when_dependency_network_is_unavailable(self):
        current = self.root / 'current'; current.mkdir()
        (current / 'cli-manifest.json').write_text(json.dumps({'format':'loginom-cli-artifact-v1'}))
        with patch.object(sys, 'argv', ['build.py', str(self.root), str(current)]), \
             patch.object(build.os, 'uname', return_value=SimpleNamespace(sysname='Linux', machine='x86_64')), \
             patch.object(build, 'managed_root', return_value=(self.root, {})), \
             patch.object(build.subprocess, 'check_output', return_value='1.3.14'), \
             patch.object(build, 'verify_candidate', return_value=SimpleNamespace(returncode=0)), \
             patch.object(build, 'ops_identity', return_value={'commit':'fixture'}), \
             patch.object(build, 'build_command', side_effect=RuntimeError('NETWORK_UNAVAILABLE')) as install, \
             redirect_stdout(StringIO()):
            build.main()
            install.assert_not_called()

    def test_three_publications_keep_only_current_after_own_temps_removed(self):
        current = self.root / 'current'
        for index in range(3):
            with tempfile.TemporaryDirectory(dir=self.root) as directory:
                temporary = Path(directory); staged = temporary / 'payload'; staged.mkdir()
                (staged / 'value').write_text(str(index)); build.publish(staged, current, temporary)
        self.assertEqual([p.name for p in self.root.iterdir()], ['current'])
        self.assertEqual((current / 'value').read_text(), '2')

    def test_account_pair_is_durable_before_ui_and_idempotent_per_card(self):
        operator = self.root / 'operator.json'
        common.write_private(operator, {'workspace_id':'workspace','agents':{'worker':'w','reviewer':'r'},
            'url':'https://test.invalid','api_key':'private','provider_auth_file':'/private/auth'})
        first = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'; second = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
        paths = provision.allocate(first, operator, self.root / 'cards')
        saved = [common.read_private(path) for path in paths]
        self.assertEqual([common.read_private(path) for path in provision.allocate(first, operator, self.root / 'cards')], saved)
        other = [common.read_private(path) for path in provision.allocate(second, operator, self.root / 'cards')]
        self.assertEqual(len({d['loginom']['username'] for d in saved + other}), 4)
        self.assertTrue(all(d['account_state'] == 'planned' for d in saved))
        self.assertTrue(all(p.stat().st_mode & 0o077 == 0 for p in paths))

    def test_unknown_owner_never_claims_existing_root(self):
        worktree = self.root / 'work'; worktree.mkdir(); (worktree / '.multica-node').mkdir()
        common.write_private(self.root / '.managed_env.json', {'managed_by':'multica-daemon-managed-env',
            'workspace_id':'w','issue_id':'i','agent_id':'a'})
        with patch.object(common.subprocess, 'check_output', return_value=str(worktree)):
            with self.assertRaisesRegex(RuntimeError, 'ARTIFACT_OWNER_UNKNOWN'):
                common.managed_root(worktree)
            self.assertTrue((worktree / '.multica-node').exists())

    def test_pass_needs_exact_cleanup_and_normal_exit(self):
        result = {'cli_exit':0,'oracle_exit':0,'oracle':{'status':'PASS'},'timed_out':False,
                  'cleanup':{'package_closed':True,'logged_out':True}}
        self.assertTrue(accept.passed(result))
        for changes in [{'cli_exit':-15},{'timed_out':True},{'cleanup':{'package_closed':True,'logged_out':False}}]:
            self.assertFalse(accept.passed({**result, **changes}))

    def test_setup_failure_retains_sanitized_logs_and_never_runs_model(self):
        worktree = self.root / 'work'; worktree.mkdir()
        root = worktree / '.multica-node'; root.mkdir()
        current = root / 'current'; current.mkdir()
        common.write_private(current / 'cli-manifest.json', {'metadata':{
            'channel':'dev', 'sourceDirty':False, 'sourceCommit':'commit', 'sourceTreeSha256':'tree'}})
        acceptance = worktree / 'docs/node-development/nodes/test/acceptance'
        (acceptance / 'data').mkdir(parents=True)
        (acceptance / 'task.md').write_text('Save {{PACKAGE_PATH}}')
        owner = {'workspace_id':'workspace','issue_id':'issue','agent_id':'worker'}
        config = self.root / 'role.json'
        common.write_private(config, {'role':'worker','model':'openai/gpt-6.1-sol','variant':'low',
            'provider_auth_file':str(self.root / 'auth.json'),
            'loginom':{'username':'worker','password':'private','api_key':'private','url':'https://test.invalid'}})
        out = root / 'attempts/failed'
        args = ['accept.py', '--worktree', str(worktree), '--node', 'test', '--config', str(config),
                '--cli', str(current / 'bin/loginom-ai-agent-cli'), '--out', str(out)]
        def setup_only(command, **kwargs):
            self.assertIn('setup', command)
            kwargs['stdout'].write(b'{"ok":false,"code":"LOGINOM_RUNTIME_START_FAILED"}\n')
            return 1
        def redact(command, **kwargs):
            (out / 'evidence/events.jsonl').write_text('sanitized setup failure\n')
            return SimpleNamespace(returncode=0)
        with patch.object(sys, 'argv', args), \
             patch.object(accept, 'managed_root', return_value=(root, owner)), \
             patch.object(accept, 'verify_candidate', return_value=SimpleNamespace(returncode=0)), \
             patch.object(accept, 'ops_identity', return_value={'commit':'fixture'}), \
             patch.object(accept, 'run', side_effect=setup_only) as run, \
             patch.object(accept.subprocess, 'run', side_effect=redact), redirect_stdout(StringIO()):
            self.assertEqual(accept.main(), 1)
            self.assertEqual(run.call_count, 1)
        result = json.loads((out / 'evidence/result.json').read_text())
        self.assertEqual(result['status'], 'FAIL')
        self.assertEqual(result['error'], 'CLI_SETUP_FAILED')
        self.assertEqual(result['oracle']['status'], 'not_run')
        self.assertTrue((out / 'evidence/events.jsonl').is_file())

    def test_mismatched_or_failed_download_never_confirms_publication(self):
        comment={'id':'comment','issue_id':'issue','attachments':[{'filename':'result.json','id':'file','comment_id':'comment'}]}
        class Fake:
            def request(self, *args, **kwargs): return b'wrong'
        with self.assertRaisesRegex(RuntimeError, 'ATTACHMENT_CONTENT_MISMATCH'):
            publisher.verify_upload(Fake(), 'issue', comment, {'result.json':'correct'})
        with self.assertRaisesRegex(RuntimeError, 'ATTACHMENT_BINDING_MISMATCH'):
            publisher.verify_upload(Fake(), 'foreign', comment, {'result.json':'correct'})

    def test_failed_upload_preserves_local_evidence_without_receipt_or_done(self):
        attempt = self.root / 'attempts' / 'one'
        evidence = attempt / 'evidence'
        evidence.mkdir(parents=True)
        owner = {'workspace_id':'workspace', 'issue_id':'issue', 'agent_id':'worker'}
        result = evidence / 'result.json'
        common.write_private(result, {**owner, 'status':'PASS', 'source_sha':'commit'})
        content = attempt / 'comment.md'; content.write_text('Verified result')
        config, auth = self.root / 'config.json', self.root / 'auth.json'
        common.write_private(config, {'provider_auth_file':str(auth),
            'loginom':{'password':'private-password', 'api_key':'private-api-key'}})
        common.write_private(auth, {})
        requests = []
        class Fake:
            status = 'in_progress'
            def request(self, path, data=None):
                requests.append((path, data))
                return {'status':self.status} if path == 'issues/issue' else []
        args = ['publish-evidence.py', '--worktree', str(self.root), '--config', str(config),
                '--attempt', str(attempt), '--content-file', str(content)]
        with patch.object(sys, 'argv', args), \
             patch.object(publisher, 'managed_root', return_value=(self.root, owner)), \
             patch.object(publisher, 'API', return_value=Fake()), \
             patch.object(publisher.subprocess, 'run', return_value=SimpleNamespace(returncode=1)):
            with self.assertRaisesRegex(RuntimeError, 'EVIDENCE_PUBLICATION_UNCONFIRMED'):
                publisher.main()
        self.assertEqual(json.loads(result.read_text())['status'], 'PASS')
        self.assertTrue((attempt / 'publication/result.json').is_file())
        self.assertFalse((attempt / 'publication-receipt.json').exists())
        self.assertTrue(all(data is None for _, data in requests))
        for status in ['done', 'cancelled']:
            api = Fake(); api.status = status
            with patch.object(sys, 'argv', args), \
                 patch.object(publisher, 'managed_root', return_value=(self.root, owner)), \
                 patch.object(publisher, 'API', return_value=api), \
                 patch.object(publisher.subprocess, 'run') as publish:
                with self.assertRaisesRegex(RuntimeError, 'COMPLETED_ISSUE_NO_NEW_ACTION'):
                    publisher.main()
                publish.assert_not_called()


if __name__ == '__main__':
    unittest.main()
