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

    def test_mismatched_or_failed_download_never_confirms_publication(self):
        comment={'id':'comment','issue_id':'issue','attachments':[{'filename':'result.json','id':'file','comment_id':'comment'}]}
        class Fake:
            def request(self, *args, **kwargs): return b'wrong'
        with self.assertRaisesRegex(RuntimeError, 'ATTACHMENT_CONTENT_MISMATCH'):
            publisher.verify_upload(Fake(), 'issue', comment, {'result.json':'correct'})
        with self.assertRaisesRegex(RuntimeError, 'ATTACHMENT_BINDING_MISMATCH'):
            publisher.verify_upload(Fake(), 'foreign', comment, {'result.json':'correct'})


if __name__ == '__main__':
    unittest.main()
