"""One foreground bwrap/Xvfb boundary; no daemon, recovery sweep or GC."""
import fcntl
import os
from pathlib import Path
import signal
import subprocess


def run(command, *, attempt, payload, cwd, profile=None, auth=None, read_only=(), writable=(), input=None, stdout=None, stderr=None, timeout=7200, pass_fds=()):
    attempt, payload, cwd = Path(attempt), Path(payload), Path(cwd)
    script = Path(__file__).resolve().parent
    lock = None
    if auth:
        lock = os.open(str(auth) + '.lock', os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        os.set_inheritable(lock, True)
    argv = ['/usr/bin/bwrap', '--die-with-parent', '--new-session', '--unshare-all', '--share-net', '--cap-drop', 'ALL', '--ro-bind', '/usr', '/usr']
    for source, target in [('usr/bin', '/bin'), ('usr/sbin', '/sbin'), ('usr/lib', '/lib'), ('usr/lib64', '/lib64')]:
        argv += ['--symlink', source, target]
    argv += ['--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp', '--tmpfs', '/run', '--tmpfs', '/dev/shm']
    for path in ['/etc/ssl', '/etc/ca-certificates', '/etc/resolv.conf', '/etc/hosts', '/etc/nsswitch.conf', '/etc/passwd', '/etc/group', '/etc/fonts', '/etc/alternatives/awk']:
        if Path(path).exists():
            argv += ['--ro-bind', path, path]
    for path in dict.fromkeys([str(payload), str(script), *map(str, read_only)]):
        argv += ['--ro-bind', path, path]
    for path in dict.fromkeys([str(attempt), *map(str, writable)]):
        argv += ['--bind', path, path]
    env = {key: value for key, value in os.environ.items() if key in ['PATH', 'LANG', 'LC_ALL', 'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy', 'no_proxy', 'NODE_EXTRA_CA_CERTS'] and value}
    argv += ['--clearenv']
    env.update({'HOME': str(attempt), 'TMPDIR': '/tmp', 'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'LOGINOM_AI_AGENT_PURE': '1', 'LOGINOM_AI_AGENT_DISABLE_PROJECT_CONFIG': '1', 'LOGINOM_AI_AGENT_SYSTEM_PROXY': 'off', 'LOGINOM_AI_AGENT_TEST_HEADLESS': '0'})
    if profile:
        env['LOGINOM_AI_AGENT_CLI_PROFILE'] = str(profile)
    if auth:
        target = Path(profile) / 'data/auth.json'
        target.touch(mode=0o600, exist_ok=True)
        argv += ['--bind', str(auth), str(target)]
    for key, value in env.items():
        argv += ['--setenv', key, value]
    argv += ['--chdir', str(cwd), '--', '/usr/bin/xvfb-run', '-a', '-s', '-screen 0 1920x1200x24 -nolisten tcp', '/usr/bin/python3', str(script / 'headed-entry.py'), *map(str, command)]
    process = None
    try:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True, pass_fds=(*pass_fds, *((lock,) if lock is not None else ())))
        try:
            process.communicate(input, timeout=timeout)
            return process.returncode
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGINT)
            try:
                process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            raise RuntimeError('COMMAND_TIMED_OUT') from None
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        if lock is not None:
            os.close(lock)
