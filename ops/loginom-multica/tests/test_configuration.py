import copy
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import common
import concurrency
import configure

spec = importlib.util.spec_from_file_location('account_provision', SCRIPTS / 'provision-accounts.py')
provision = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provision)


class API:
    def __init__(self):
        self.agents = {role: {'id': role, 'max_concurrent_tasks': 2, 'model': 'keep-model',
                             'instructions': 'old', 'mcp_config': {'secret': 'keep'},
                             'runtime_config': {'mode': 'keep'}, 'runtime_id': 'runtime',
                             'permission_mode': 'private', 'custom_args': ['keep'],
                             'updated_at': 'before'}
                       for role in ['generator', 'worker', 'reviewer']}
        self.squad = {'id': 'squad', 'instructions': 'old', 'leader_id': 'keep-leader',
                      'updated_at': 'before', 'name': 'keep-name'}
        self.tasks = []
        self.issue = {'id': 'maintenance', 'workspace_id': 'workspace',
                      'assignee_type': 'agent', 'assignee_id': 'worker'}
        self.writes = []
        self.resources = {'resources': [{'id': 'resource', 'resource_ref': {'ref': 'keep-ref'}}]}

    def request(self, path, data=None):
        if path == 'agent-task-snapshot':
            return copy.deepcopy(self.tasks)
        if path == 'issues/maintenance':
            return copy.deepcopy(self.issue)
        if path == 'projects/project/resources':
            return copy.deepcopy(self.resources)
        target = self.agents[path[7:]] if path.startswith('agents/') else self.squad
        if data is not None:
            self.writes.append((path, copy.deepcopy(data)))
            target.update(data)
            target['updated_at'] = 'after'
        return copy.deepcopy(target)


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.api = API()
        self.deployment = {'workspace_id': 'workspace', 'agents': {role: role for role in self.api.agents}}
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_eight_limits_preserve_other_settings(self):
        limits = concurrency.set_limits(self.api, self.deployment, 8)
        self.assertEqual(limits, {'generator': 1, 'worker': 8, 'reviewer': 8})
        self.assertEqual([payload for _, payload in self.api.writes],
                         [{'max_concurrent_tasks': 1}, {'max_concurrent_tasks': 8}, {'max_concurrent_tasks': 8}])
        for agent in self.api.agents.values():
            self.assertEqual(agent['model'], 'keep-model')
            self.assertEqual(agent['mcp_config'], {'secret': 'keep'})

    def test_invalid_limit_is_rejected_before_api_mutation(self):
        for value in [0, 9, True, 1.5]:
            with self.assertRaisesRegex(RuntimeError, 'CONCURRENCY_LIMIT_INVALID'):
                concurrency.set_limits(self.api, self.deployment, value)
        self.assertEqual(self.api.writes, [])

    def maintenance(self):
        self.deployment['maintenance_issue_id'] = 'maintenance'
        os.environ.update({'MULTICA_TASK_ID': 'current-task', 'MULTICA_AGENT_ID': 'worker',
                           'MULTICA_WORKSPACE_ID': 'workspace'})
        self.api.tasks = [{'id': 'current-task', 'agent_id': 'worker', 'workspace_id': 'workspace',
                           'issue_id': 'maintenance', 'status': 'running'}]

    def test_only_exact_current_native_maintenance_run_is_exempt(self):
        self.maintenance()
        concurrency.ensure_idle(self.api, self.deployment)
        self.api.tasks.append({'id': 'peer', 'agent_id': 'reviewer', 'status': 'running'})
        with self.assertRaisesRegex(RuntimeError, 'WORKSPACE_NOT_IDLE'):
            concurrency.set_limits(self.api, self.deployment, 8)
        self.assertEqual(self.api.writes, [])

    def test_maintenance_identity_mismatches_fail_closed(self):
        for field, value in [('id', 'other'), ('workspace_id', 'foreign'), ('agent_id', 'reviewer'),
                             ('issue_id', 'ordinary-card'), ('status', 'queued')]:
            self.maintenance()
            self.api.tasks[0][field] = value
            with self.assertRaisesRegex(RuntimeError, 'WORKSPACE_NOT_IDLE'):
                concurrency.ensure_idle(self.api, self.deployment)
        self.maintenance()
        self.api.issue['assignee_id'] = 'reviewer'
        with self.assertRaisesRegex(RuntimeError, 'MAINTENANCE_IDENTITY_UNCONFIRMED'):
            concurrency.ensure_idle(self.api, self.deployment)
        self.assertEqual(self.api.writes, [])

    def test_environment_without_registered_maintenance_issue_never_exempts(self):
        self.maintenance()
        del self.deployment['maintenance_issue_id']
        with self.assertRaisesRegex(RuntimeError, 'WORKSPACE_NOT_IDLE'):
            concurrency.ensure_idle(self.api, self.deployment)

    def test_instruction_reload_changes_only_instructions_and_preserves_resource(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'instructions').mkdir()
            for role in ['generator', 'worker', 'reviewer', 'squad']:
                (root / 'instructions' / (role + '.md')).write_text('new @@OPS@@')
            deployment = {**self.deployment, 'squad_id': 'squad', 'ops_path': '/ops',
                          'project_id': 'project', 'resource_id': 'resource',
                          'max_concurrent_tasks': {'generator': 1, 'worker': 8, 'reviewer': 8}}
            resources = copy.deepcopy(self.api.resources)
            configure.configure(self.api, deployment, root, instructions_only=True)
        self.assertTrue(all(payload == {'instructions': 'new /ops'} for _, payload in self.api.writes))
        self.assertEqual(self.api.squad['leader_id'], 'keep-leader')
        self.assertEqual({role: value['max_concurrent_tasks'] for role, value in self.api.agents.items()},
                         {'generator': 2, 'worker': 2, 'reviewer': 2})
        self.assertEqual(self.api.resources, resources)

    def test_instruction_readback_detects_unrelated_settings_mutation(self):
        request = self.api.request
        def change_permissions(path, data=None):
            result = request(path, data)
            if data is not None and path.startswith('agents/'):
                self.api.agents[path[7:]]['permission_mode'] = 'public_to'
            return result
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'instructions').mkdir()
            for role in ['generator', 'worker', 'reviewer', 'squad']:
                (root / 'instructions' / (role + '.md')).write_text('new')
            deployment = {**self.deployment, 'squad_id': 'squad', 'ops_path': '/ops'}
            with patch.object(self.api, 'request', side_effect=change_permissions):
                with self.assertRaisesRegex(RuntimeError, 'AGENT_SETTINGS_CHANGED'):
                    configure.configure(self.api, deployment, root, instructions_only=True)

    def test_role_config_pool_refresh_preserves_existing_loginom_accounts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            operator = root / 'operator.json'
            issue = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
            pool = ['/private/account-' + str(index) + '/auth.json' for index in range(8)]
            common.write_private(operator, {'workspace_id': 'workspace', 'agents': {'worker': 'worker', 'reviewer': 'reviewer'},
                                           'url': 'https://test.invalid', 'api_key': 'private', 'provider_auth_files': pool})
            paths = provision.allocate(issue, operator, root / 'cards')
            saved = [common.read_private(path) for path in paths]
            for path in paths:
                config = common.read_private(path)
                config.pop('provider_auth_files')
                config['provider_auth_file'] = '/old/auth.json'
                config['account_state'] = 'ready'
                common.write_private(path, config)
            provision.allocate(issue, operator, root / 'cards')
            actual = [common.read_private(path) for path in paths]
            self.assertEqual([config['loginom'] for config in actual], [config['loginom'] for config in saved])
            self.assertTrue(all(config['account_state'] == 'ready' for config in actual))
            self.assertTrue(all(config['provider_auth_files'] == pool and 'provider_auth_file' not in config for config in actual))
