import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from common import write_private
from provider_pool import loginom_lock


class AccountCommandTests(unittest.TestCase):
    def test_foreground_command_borrows_lock_without_reacquiring(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config = {'operator_file': str(root / 'operator.json'), 'loginom': {'url': 'https://loginom.invalid', 'username': 'one'}}
            path = root / 'worker.json'
            write_private(path, config)
            child = root / 'child.py'
            child.write_text('import os,sys\nsys.path.insert(0,sys.argv[1])\nfrom common import read_private\nfrom provider_pool import inherited_account_lock\ninherited_account_lock(read_private(sys.argv[2]),int(os.environ["LOGINOM_MULTICA_ACCOUNT_FD"]),sys.argv[2])\n')
            command = [sys.executable, str(SCRIPTS / 'account-lease.py'), '--config', str(path), '--timeout', '2', '--', sys.executable, str(child), str(SCRIPTS), str(path)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            os.close(loginom_lock(config, deadline=time.monotonic()+1, config_path=path))

    def test_command_timeout_terminates_owned_process_and_frees_account(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config = {'operator_file': str(root / 'operator.json'), 'loginom': {'url': 'https://loginom.invalid', 'username': 'one'}}
            path = root / 'worker.json'
            write_private(path, config)
            command = [sys.executable, str(SCRIPTS / 'account-lease.py'), '--config', str(path), '--timeout', '0.15', '--', sys.executable, '-c', 'import time;time.sleep(60)']
            result = subprocess.run(command, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 1)
            self.assertIn('ACCOUNT_COMMAND_TIMED_OUT', result.stderr)
            os.close(loginom_lock(config, deadline=time.monotonic()+1, config_path=path))


if __name__ == '__main__':
    unittest.main()
