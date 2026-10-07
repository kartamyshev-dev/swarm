#!/usr/bin/env python3
"""A window manager for the lifetime of one foreground browser command."""
import subprocess
import sys
import time
import os
import stat
from pathlib import Path

leases = tuple(int(value) for value in os.environ.pop('LOGINOM_MULTICA_LEASE_FDS', '').split(',') if value)
if any(fd < 3 or not stat.S_ISREG(os.fstat(fd).st_mode) for fd in leases):
    raise RuntimeError('LEASE_DESCRIPTOR_INVALID')
wm = subprocess.Popen(['/usr/bin/openbox', '--config-file', str(Path(__file__).with_name('openbox.xml'))], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(50):
        check = subprocess.run(['/usr/bin/xprop', '-root', '_NET_SUPPORTING_WM_CHECK'], capture_output=True, text=True)
        if 'window id' in check.stdout:
            break
        if wm.poll() is not None:
            raise RuntimeError('WINDOW_MANAGER_EXITED')
        time.sleep(0.1)
    else:
        raise RuntimeError('WINDOW_MANAGER_NOT_READY')
    result = subprocess.run(sys.argv[1:], pass_fds=leases)
finally:
    wm.terminate()
    try:
        wm.wait(timeout=5)
    except subprocess.TimeoutExpired:
        wm.kill()
        wm.wait()
raise SystemExit(result.returncode)
