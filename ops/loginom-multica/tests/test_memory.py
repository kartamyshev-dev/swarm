import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import configure
import memory


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.config = {'url': memory.URL, 'peer_id': memory.PEER, 'api_key': 'test-key'}

    def test_other_project_or_server_is_rejected(self):
        for changed in [{'peer_id': 'foreign'}, {'url': 'https://foreign.invalid'}]:
            with self.assertRaisesRegex(RuntimeError, 'PROJECT_CONFIG_MISMATCH'):
                memory.mcp_server({**self.config, **changed})

    def test_configure_preserves_other_mcp_servers_and_project_ref(self):
        class API:
            agent = {'mcp_config': {'mcpServers': {'existing': {'command': 'keep'}}}}
            squad = {}
            resource = {'id': 'resource'}

            def request(self, path, data=None):
                if path == 'me':
                    return {'id': 'owner'}
                if path == 'agents/agent':
                    if data: self.agent.update(data)
                    return self.agent
                if path == 'squads/squad':
                    if data: self.squad.update(data)
                    return self.squad
                if path.endswith('/resources/resource'):
                    self.resource.update(data)
                    return self.resource
                return {'resources': [self.resource]}

        api = API()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'instructions').mkdir()
            for name in ['generator', 'worker', 'reviewer', 'squad']:
                (root / 'instructions' / (name + '.md')).write_text('instructions @@OWNER@@')
            deployment = {'ops_path': '/ops', 'agents': {'generator': 'agent', 'worker': 'agent', 'reviewer': 'agent'},
                          'squad_id': 'squad', 'project_id': 'project', 'resource_id': 'resource', 'repository': 'repo'}
            configure.configure(api, deployment, root, 'current-sha', self.config)
        servers = api.agent['mcp_config']['mcpServers']
        self.assertEqual(servers['existing'], {'command': 'keep'})
        self.assertEqual(servers['openviking']['headers']['X-OpenViking-Actor-Peer'], memory.PEER)
        self.assertEqual(api.resource['resource_ref']['ref'], 'current-sha')
        self.assertEqual(api.agent['instructions'], 'instructions owner')
        self.assertEqual(api.squad['instructions'], 'instructions owner')

    def test_note_sends_explicit_peer_and_does_not_claim_extraction_complete(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / 'receipt.json'
            def request(config, path, data):
                self.assertTrue(receipt.exists())
                calls.append((path, data))
                return {'task_id': 'task'} if path.endswith('/commit') else {}
            with patch.object(memory, 'request', side_effect=request):
                result = memory.save_note(self.config, 'Confirmed project fact', receipt)
            self.assertEqual(calls[0][1]['peer_id'], memory.PEER)
            self.assertEqual(result['status'], 'submitted')
            with self.assertRaisesRegex(RuntimeError, 'NEW_MEMORY_RECEIPT_REQUIRED'):
                memory.save_note(self.config, 'Confirmed project fact', receipt)

    def test_commit_failure_keeps_session_receipt_for_inspection(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / 'receipt.json'
            with patch.object(memory, 'request', side_effect=[{}, RuntimeError('UNAVAILABLE')]):
                with self.assertRaisesRegex(RuntimeError, 'UNAVAILABLE'):
                    memory.save_note(self.config, 'Confirmed project fact', receipt)
            saved = json.loads(receipt.read_text())
            self.assertEqual(saved['status'], 'message_saved')
            self.assertEqual(saved['peer_id'], memory.PEER)
            self.assertTrue(saved['session_id'].startswith('multica-note-'))
            self.assertEqual(receipt.stat().st_mode & 0o077, 0)
