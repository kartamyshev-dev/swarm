"""Private OAuth directory and one permanent interprocess lock; no secret copies."""
import fcntl
import json
import os
from pathlib import Path
import stat
import re

CAPABILITY = 'shared-oauth-v1'
MARKER = {'format': 'loginom-shared-oauth-v1'}


def private_path(path, *, directory=False):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path or path.is_symlink():
        raise RuntimeError('AUTH_PATH_INVALID')
    info = path.stat()
    correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not correct_type or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600) or (not directory and info.st_nlink != 1):
        raise RuntimeError('AUTH_PERMISSIONS_INVALID')
    return path


def shared_directory(auth):
    auth = private_path(auth)
    directory = auth.parent
    marker = directory / 'marker.json'
    if not marker.exists():
        return None
    private_path(directory, directory=True)
    if auth.name != 'auth.json' or json.loads(private_path(marker).read_text()) != MARKER:
        raise RuntimeError('SHARED_AUTH_MARKER_INVALID')
    for entry in directory.iterdir():
        temporary = re.fullmatch(r'(auth|refresh-pending)\.json\.tmp-[0-9a-fA-F-]{36}|marker\.json\.tmp-[0-9]+', entry.name)
        if entry.name not in ['auth.json', 'auth.json.lock', 'marker.json', 'refresh-pending.json'] and not temporary:
            raise RuntimeError('SHARED_AUTH_DIRECTORY_NOT_DEDICATED')
        try:
            private_path(entry)
        except FileNotFoundError:
            if not temporary and entry.name != 'refresh-pending.json':
                raise
    private_path(directory / 'auth.json.lock')
    return directory


def auth_lock(auth, *, nonblocking=False):
    private_path(auth)
    path = Path(str(auth) + '.lock')
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        private_path(path)
        mode = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
        fcntl.flock(fd, mode)
        private_path(auth)
        os.set_inheritable(fd, True)
        return fd
    except BlockingIOError:
        os.close(fd)
        raise RuntimeError('AUTH_BUSY') from None
    except BaseException:
        os.close(fd)
        raise


def auth_mode(auth, capabilities):
    auth = Path(auth)
    for parent in [auth.parent, auth.parent.parent]:
        journal = parent / 'parallel-auth-migration.json'
        if journal.exists():
            data = json.loads(private_path(journal).read_text())
            if str(auth) in [data.get('source'), data.get('target')] and data.get('state') != 'complete':
                raise RuntimeError('AUTH_MIGRATION_INCOMPLETE')
    directory = shared_directory(auth)
    if directory and isinstance(capabilities, (list, tuple)) and CAPABILITY in capabilities:
        return 'shared-refresh', directory
    return 'serial', directory
