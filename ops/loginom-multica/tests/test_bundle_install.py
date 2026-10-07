import hashlib
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
spec = importlib.util.spec_from_file_location('bundle_installer', SCRIPTS / 'install-bundle.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class API:
    def __init__(self):
        self.tasks = [{'id': 'native-task', 'workspace_id': 'workspace', 'agent_id': 'worker',
                       'issue_id': 'maintenance', 'status': 'running'}]

    def request(self, path):
        if path == 'agent-task-snapshot':
            return self.tasks
        if path == 'issues/maintenance':
            return {'id': 'maintenance', 'workspace_id': 'workspace',
                    'assignee_id': 'worker', 'assignee_type': 'agent'}
        return {'id': path.removeprefix('agents/')}


class BundleInstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.bundle = self.root / 'export'
        self.target = self.root / 'installed'
        self.commit = 'a' * 40
        self.previous = 'b' * 40
        self.version = self.bundle_at(self.bundle, self.commit, 'new')
        self.old = self.bundle_at(self.target, self.previous, 'old')
        self.api = API()
        self.deployment = {'workspace_id': 'workspace', 'maintenance_issue_id': 'maintenance',
                           'agents': {role: role for role in ['generator', 'worker', 'reviewer']}}
        environment = patch.dict(os.environ, {'MULTICA_TASK_ID': 'native-task', 'MULTICA_AGENT_ID': 'worker',
                                             'MULTICA_WORKSPACE_ID': 'workspace'}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def bundle_at(self, root, commit, value):
        root.mkdir(mode=0o700)
        files = {'scripts/tool.py': value.encode(), 'instructions/worker.md': b'instructions'}
        for name, body in files.items():
            path = root / name
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_bytes(body)
        inventory = {name: hashlib.sha256(body).hexdigest() for name, body in files.items()}
        digest = hashlib.sha256(json.dumps(inventory, sort_keys=True).encode()).hexdigest()
        version = {'commit': commit, 'bundle_sha256': digest, 'files': inventory}
        (root / 'VERSION.json').write_text(json.dumps(version))
        return version

    def install(self):
        return installer.install(self.api, self.deployment, self.bundle, self.target,
                                 self.commit, self.version['bundle_sha256'])

    def assert_old(self):
        self.assertEqual(installer.verify_bundle(self.target), self.old)
        self.assertEqual((self.target / 'scripts/tool.py').read_text(), 'old')

    def test_valid_install_verifies_identity_permissions_and_retains_previous(self):
        (self.target / 'scripts/__pycache__').mkdir(mode=0o700)
        (self.target / 'scripts/__pycache__/old.pyc').write_bytes(b'old generated cache')
        result = self.install()
        self.assertEqual(result['readback'], 'verified')
        self.assertEqual(installer.verify_bundle(self.target), self.version)
        backup = Path(result['previous_bundle'])
        self.assertEqual(installer.verify_bundle(backup, exact=False), self.old)
        self.assertEqual((backup / 'scripts/__pycache__/old.pyc').read_bytes(), b'old generated cache')
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.target / 'scripts/tool.py').stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.target / 'VERSION.json').stat().st_mode & 0o777, 0o600)

    def test_tampered_bundle_refused_before_target_mutation(self):
        (self.bundle / 'scripts/tool.py').write_text('tampered')
        with self.assertRaisesRegex(RuntimeError, 'BUNDLE_FILE_HASH_MISMATCH'):
            self.install()
        self.assert_old()
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ['export', 'installed'])

    def test_traversal_inventory_refused_before_target_mutation(self):
        version = {**self.version, 'files': {'../outside': '0' * 64}}
        (self.bundle / 'VERSION.json').write_text(json.dumps(version))
        with self.assertRaisesRegex(RuntimeError, 'BUNDLE_INVENTORY_INVALID'):
            self.install()
        self.assert_old()

    def test_extra_files_or_links_refused_before_target_mutation(self):
        extra = self.bundle / 'secret.json'
        extra.write_text('extra')
        with self.assertRaisesRegex(RuntimeError, 'BUNDLE_INVENTORY_MISMATCH'):
            self.install()
        extra.unlink()
        source = self.bundle / 'scripts/tool.py'
        source.unlink()
        source.symlink_to(self.target / 'scripts/tool.py')
        with self.assertRaisesRegex(RuntimeError, 'BUNDLE_LINK_OR_OWNER_INVALID'):
            self.install()
        self.assert_old()

    def test_exact_commit_and_digest_are_required(self):
        for commit, digest in [('c' * 40, self.version['bundle_sha256']), (self.commit, 'd' * 64)]:
            with self.assertRaisesRegex(RuntimeError, 'BUNDLE_IDENTITY_MISMATCH'):
                installer.install(self.api, self.deployment, self.bundle, self.target, commit, digest)
        self.assert_old()

    def test_second_rename_failure_restores_previous_version(self):
        actual = os.replace
        def fail_new(source, destination):
            if '-staging-' in Path(source).name and Path(destination) == self.target:
                raise OSError('simulated publication failure')
            return actual(source, destination)
        with patch.object(installer.os, 'replace', side_effect=fail_new):
            with self.assertRaises(OSError):
                self.install()
        self.assert_old()
        self.assertFalse(any('-staging-' in path.name or '-previous-' in path.name for path in self.root.iterdir()))

    def test_missing_previous_inventory_never_replaces_foreign_directory(self):
        (self.target / 'VERSION.json').unlink()
        with self.assertRaisesRegex(RuntimeError, 'BUNDLE_VERSION_INVALID'):
            self.install()
        self.assertEqual((self.target / 'scripts/tool.py').read_text(), 'old')

    def test_peer_task_appearing_before_switch_blocks_install(self):
        actual = installer.maintenance
        calls = 0
        def peer_appears(api, deployment):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.api.tasks.append({'id': 'peer', 'status': 'running'})
            actual(api, deployment)
        with patch.object(installer, 'maintenance', side_effect=peer_appears):
            with self.assertRaisesRegex(RuntimeError, 'WORKSPACE_NOT_IDLE'):
                self.install()
        self.assert_old()

    def test_operator_outside_native_task_cannot_install_even_if_idle(self):
        self.api.tasks = []
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'NATIVE_MAINTENANCE_REQUIRED'):
                self.install()
        self.assert_old()


if __name__ == '__main__':
    unittest.main()
