import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from common import read_private, write_private


class ProvisionProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.issue = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        self.card = self.root / 'cards' / self.issue
        self.worker = self.card / 'worker.json'
        self.operator = self.root / 'operator.json'
        self.ready = self.root / 'ready.json'
        self.stopped = self.root / 'signal.txt'
        self.child = self.root / 'browser.py'
        self.child.write_text(
            'import json,os,pathlib,signal,sys,time\n'
            'def stop(signum,frame):\n'
            ' if not pathlib.Path(sys.argv[2]).exists(): pathlib.Path(sys.argv[2]).write_text(str(signum))\n'
            'signal.signal(signal.SIGTERM,stop)\n'
            'signal.signal(signal.SIGINT,stop)\n'
            'pathlib.Path(sys.argv[1]).write_text(json.dumps({"pid":os.getpid(),"pgid":os.getpgrp()}))\n'
            'print("FAKE_SECRET_DONT_ECHO",flush=True)\n'
            'print("FAKE_SECRET_DONT_ECHO",file=sys.stderr,flush=True)\n'
            'while True: time.sleep(0.05)\n')
        self.node = self.root / 'fake-node'
        self.node.write_text(
            '#!' + sys.executable + '\n'
            'import os,pathlib,subprocess,sys,time\n'
            'sys.path.insert(0,' + repr(str(SCRIPTS)) + ')\n'
            'from common import read_private,write_private\n'
            'config=pathlib.Path(sys.argv[sys.argv.index("--config")+1])\n'
            'write_private(config,{**read_private(config),"account_state":"creating"})\n'
            'identity=(config.parent/".accounts.lock").stat()\n'
            'fds=[]\n'
            'for name in os.listdir("/dev/fd"):\n'
            ' try:\n'
            '  fd=int(name); info=os.fstat(fd)\n'
            '  if (info.st_dev,info.st_ino)==(identity.st_dev,identity.st_ino): fds.append(fd)\n'
            ' except OSError: pass\n'
            'assert len(fds)==1,"INHERITED_LOCK_MISSING"\n'
            'subprocess.Popen([' + repr(sys.executable) + ',' + repr(str(self.child)) + ',' + repr(str(self.ready)) + ',' + repr(str(self.stopped)) + '],pass_fds=tuple(fds))\n'
            'print("FAKE_SECRET_DONT_ECHO",flush=True)\n'
            'print("FAKE_SECRET_DONT_ECHO",file=sys.stderr,flush=True)\n'
            'while True: time.sleep(0.05)\n')
        self.node.chmod(0o700)
        write_private(self.operator, {'workspace_id': 'workspace', 'agents': {'worker': 'worker', 'reviewer': 'reviewer'},
                                     'url': 'https://test.invalid', 'api_key': 'private', 'node': str(self.node)})
        self.driver = self.root / 'driver.py'
        self.driver.write_text(
            'import importlib.util,pathlib,sys\n'
            'sys.path.insert(0,' + repr(str(SCRIPTS)) + ')\n'
            'spec=importlib.util.spec_from_file_location("provision",' + repr(str(SCRIPTS / 'provision-accounts.py')) + ')\n'
            'module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)\n'
            'actual=module.run_foreground\n'
            'timeout=float(sys.argv[1])\n'
            'module.run_foreground=lambda command,lock:actual(command,lock,timeout=timeout,stop_timeout=0.6)\n'
            'sys.argv=["provision-accounts.py","--issue",' + repr(self.issue) + ',"--operator",' + repr(str(self.operator)) + ',"--directory",' + repr(str(self.card.parent)) + ',"--stage","stage0"]\n'
            'try:module.main()\n'
            'except RuntimeError as error:\n'
            ' print(str(error),file=sys.stderr);sys.exit(1)\n')
        self.process = None
        self.addCleanup(self.cleanup_processes)

    def cleanup_processes(self):
        if self.ready.exists():
            group = json.loads(self.ready.read_text())['pgid']
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if self.process and self.process.poll() is None:
            self.process.kill()
            self.process.communicate(timeout=3)

    def wait_file(self, path):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if path.exists() and path.stat().st_size:
                return
            if self.process.poll() is not None:
                output = self.process.communicate()
                self.fail('Provision exited before child checkpoint: ' + repr(output))
            time.sleep(0.01)
        self.fail('Child checkpoint timed out')

    def lock_busy(self):
        descriptor = os.open(self.card / '.accounts.lock', os.O_RDWR)
        try:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(descriptor)

    def assert_finished(self, expected):
        stdout, stderr = self.process.communicate(timeout=5)
        self.assertEqual(self.process.returncode, 1)
        self.assertIn(expected, stderr)
        self.assertNotIn('FAKE_SECRET_DONT_ECHO', stdout + stderr)
        self.assertEqual(read_private(self.worker)['account_state'], 'creating')
        self.assertEqual(read_private(self.card / 'reviewer.json')['account_state'], 'planned')
        descriptor = os.open(self.card / '.accounts.lock', os.O_RDWR)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(descriptor)
        pid = json.loads(self.ready.read_text())['pid']
        status = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='], capture_output=True, text=True)
        self.assertTrue(not status.stdout.strip() or status.stdout.strip().startswith('Z'), status.stdout)

    def test_timeout_stops_browser_group_before_releasing_accounts(self):
        self.process = subprocess.Popen([sys.executable, str(self.driver), '0.4'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.wait_file(self.ready)
        self.lock_busy()
        self.wait_file(self.stopped)
        self.lock_busy()
        self.assertEqual(self.stopped.read_text(), str(signal.SIGTERM))
        self.assert_finished('ACCOUNT_PROVISION_TIMEOUT')

    def test_external_sigterm_is_forwarded_and_lock_survives_direct_child_exit(self):
        self.cancel(signal.SIGTERM)

    def test_external_sigint_is_forwarded_to_browser_group(self):
        self.cancel(signal.SIGINT)

    def cancel(self, signum):
        self.process = subprocess.Popen([sys.executable, str(self.driver), '10'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.wait_file(self.ready)
        self.lock_busy()
        self.process.send_signal(signum)
        self.wait_file(self.stopped)
        self.lock_busy()
        self.assertEqual(self.stopped.read_text(), str(signum))
        self.assert_finished('ACCOUNT_PROVISION_CANCELLED')

    def test_successful_foreground_roles_finish_without_exposing_child_output(self):
        self.node.write_text(
            '#!' + sys.executable + '\n'
            'import pathlib,sys\n'
            'sys.path.insert(0,' + repr(str(SCRIPTS)) + ')\n'
            'from common import read_private,write_private\n'
            'config=pathlib.Path(sys.argv[sys.argv.index("--config")+1])\n'
            'write_private(config,{**read_private(config),"account_state":"ready"})\n'
            'print("FAKE_SECRET_DONT_ECHO")\n'
            'print("FAKE_SECRET_DONT_ECHO",file=sys.stderr)\n')
        self.process = subprocess.Popen([sys.executable, str(self.driver), '3'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stdout, stderr = self.process.communicate(timeout=5)
        self.assertEqual(self.process.returncode, 0, stderr)
        self.assertNotIn('FAKE_SECRET_DONT_ECHO', stdout + stderr)
        self.assertTrue(json.loads(stdout)['verified'])
        self.assertEqual([read_private(path)['account_state'] for path in [self.worker, self.card / 'reviewer.json']], ['ready', 'ready'])


if __name__ == '__main__':
    unittest.main()
