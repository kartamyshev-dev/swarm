#!/usr/bin/env python3
"""A window manager for the lifetime of one foreground browser command."""
import subprocess
import sys
import time
from pathlib import Path

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
    result = subprocess.run(sys.argv[1:])
finally:
    wm.terminate()
    try:
        wm.wait(timeout=5)
    except subprocess.TimeoutExpired:
        wm.kill()
        wm.wait()
raise SystemExit(result.returncode)
