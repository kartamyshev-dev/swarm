import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import accept
import common


class AcceptanceDeadlineTests(unittest.TestCase):
    def test_stage_caps_preserve_extended_oracle_and_wait_consumes_attempt_deadline(self):
        cases = [
            (None, 0, [240, 7200, 180, 1800], [240, 7200, 180, 1800], 9420),
            (None, 1900, [240, 7200, 180, 80], [240, 7200, 180, 80], 9600),
            (1000, 100, [100, 500, 100, 200], [240, 800, 180, 200], 1000),
        ]
        for timeout, wait, elapsed, expected, duration in cases:
            with self.subTest(timeout=timeout, wait=wait), tempfile.TemporaryDirectory() as directory:
                base = Path(directory).resolve()
                worktree = base / 'work'; worktree.mkdir()
                root = worktree / '.multica-node'; root.mkdir()
                current = root / 'current'; current.mkdir()
                common.write_private(current / 'cli-manifest.json', {'metadata': {'channel': 'dev', 'sourceDirty': False, 'sourceCommit': 'commit', 'sourceTreeSha256': 'tree'}})
                acceptance = worktree / 'docs/node-development/nodes/test/acceptance'
                (acceptance / 'data').mkdir(parents=True)
                (acceptance / 'task.md').write_text('Save {{PACKAGE_PATH}}')
                (acceptance / 'expected.json').write_text('{}')
                auths = []
                for index in range(8):
                    profile = base / str(index); profile.mkdir(mode=0o700)
                    data = profile / 'data'; data.mkdir(mode=0o700)
                    auth = data / 'auth.json'
                    common.write_private(auth, {'openai': {'type': 'oauth', 'access': 'fake-access-' + str(index), 'refresh': 'fake-refresh-' + str(index), 'expires': 0}})
                    auths.append(str(auth))
                operator = base / 'operator.json'
                common.write_private(operator, {'url': 'https://loginom.invalid', 'admin_user': 'admin', 'admin_password': 'secret'})
                config = base / 'role.json'
                common.write_private(config, {'role': 'worker', 'model': 'openai/test', 'variant': 'low', 'provider_auth_files': auths,
                    'operator_file': str(operator), 'loginom': {'url': 'https://loginom.invalid', 'username': 'worker', 'password': 'private', 'api_key': 'private'}})
                out = root / 'attempts/pass'
                args = ['accept.py', '--worktree', str(worktree), '--node', 'test', '--config', str(config), '--cli', str(current / 'bin/loginom-ai-agent-cli'), '--out', str(out)]
                if timeout is not None:
                    args += ['--timeout', str(timeout)]
                clock, observed = [0], []
                actual_acquire = accept.acquire
                def acquire(*args, **kwargs):
                    clock[0] += wait
                    return actual_acquire(*args, **kwargs)
                def run(command, **kwargs):
                    observed.append(kwargs['timeout'])
                    clock[0] += elapsed[len(observed) - 1]
                    if Path(command[1]).name == 'cold-check.mjs':
                        common.write_private(out / 'evidence/oracle/result.json', {'status': 'PASS', 'cleanup': {'package_closed': True, 'logged_out': True}})
                    return 0
                def redact(command, **kwargs):
                    (out / 'evidence/events.jsonl').write_text('sanitized\n')
                    (out / 'evidence/stderr.txt').write_text('')
                    return SimpleNamespace(returncode=0)
                with patch.object(sys, 'argv', args), patch.object(accept, 'managed_root', return_value=(root, {})), \
                     patch.object(accept, 'verify_candidate', return_value=SimpleNamespace(returncode=0)), \
                     patch.object(accept, 'ops_identity', return_value={'commit': 'fixture'}), \
                     patch.object(accept.time, 'monotonic', side_effect=lambda: clock[0]), \
                     patch.object(accept, 'acquire', side_effect=acquire), \
                     patch.object(accept, 'run', side_effect=run), patch.object(accept.subprocess, 'run', side_effect=redact), redirect_stdout(StringIO()):
                    self.assertEqual(accept.main(), 0)
                self.assertEqual(observed, expected)
                result = json.loads((out / 'evidence/result.json').read_text())
                self.assertEqual(result['duration_s'], duration)
                self.assertEqual(result['timeouts_s']['oracle'], 1800)
                self.assertEqual(result['timeouts_s']['model'], 7200)
                self.assertEqual(result['timeouts_s']['attempt'], timeout if timeout is not None else 9600)


if __name__ == '__main__':
    unittest.main()
