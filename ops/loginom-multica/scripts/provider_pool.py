"""Independent persistent OAuth leases and stable Loginom account ownership."""
import fcntl
import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import time
from urllib.parse import urlsplit, urlunsplit
from common import write_private
from shared_auth import auth_lock, private_path


def _auth(auth):
    try:
        data = json.loads(private_path(auth).read_text())
        value = data['openai']
        if (not isinstance(data, dict) or set(data) != {'openai'} or
                not isinstance(value, dict) or value.get('type') != 'oauth' or
                any(not isinstance(value.get(key), str) or not value[key] for key in ['access', 'refresh']) or
                type(value.get('expires')) is not int or value['expires'] < 0):
            raise ValueError()
        return value
    except (KeyError, TypeError, ValueError, OSError):
        raise RuntimeError('PROVIDER_AUTH_INVALID') from None


def user_identity(value):
    # Match the official Codex auth claims, without treating a shared workspace as a user.
    try:
        parts = value['access'].split('.')
        if len(parts) != 3 or not all(parts):
            return None
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + '=' * (-len(parts[1]) % 4)))
        auth = claims.get('https://api.openai.com/auth', {})
        user = auth.get('chatgpt_user_id') or auth.get('user_id')
        return user if isinstance(user, str) and user and user.strip() == user else None
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def validate_catalog(paths, *, expected_count=8, inspect_auth=True, allow_unavailable=False):
    if not isinstance(paths, list) or len(paths) != expected_count:
        raise RuntimeError('PROVIDER_AUTH_CATALOG_INVALID')
    checked, identities = [], set()
    for value in paths:
        auth = Path(value)
        if not auth.is_absolute() or auth.resolve() != auth or auth.is_symlink():
            raise RuntimeError('AUTH_PATH_INVALID')
        if auth in checked:
            raise RuntimeError('PROVIDER_AUTH_DUPLICATE')
        try:
            info = auth.stat()
            if info.st_nlink != 1 or not stat.S_ISREG(info.st_mode):
                raise RuntimeError('AUTH_PATH_INVALID')
            private_path(auth)
            private_path(auth.parent, directory=True)
            private_path(auth.parent.parent, directory=True)
        except (FileNotFoundError, RuntimeError) as error:
            if not allow_unavailable or (isinstance(error, RuntimeError) and str(error) != 'AUTH_PERMISSIONS_INVALID'):
                raise
            info = None
        if (auth.parent / 'marker.json').exists() or auth.name != 'auth.json':
            raise RuntimeError('PROVIDER_AUTH_PROFILE_NOT_INDEPENDENT')
        identity = (info.st_dev, info.st_ino) if info else None
        if identity and identity in identities:
            raise RuntimeError('PROVIDER_AUTH_DUPLICATE')
        checked.append(auth)
        if identity:
            identities.add(identity)
    if not inspect_auth:
        return checked
    locks = []
    try:
        # Catalog qualification happens at rest: never read a token during its in-place refresh.
        for auth in checked:
            locks.append(auth_lock(auth, nonblocking=True))
        tokens = [_auth(auth) for auth in checked]
        if len({value['refresh'] for value in tokens}) != len(tokens) or len({value['access'] for value in tokens}) != len(tokens):
            raise RuntimeError('PROVIDER_AUTH_DUPLICATE')
        users = [user_identity(value) for value in tokens]
        if any(value is None for value in users):
            raise RuntimeError('PROVIDER_AUTH_IDENTITY_UNVERIFIED')
        if len(set(users)) != len(users):
            raise RuntimeError('PROVIDER_AUTH_DUPLICATE_USER')
        if any(_health(auth) is not None for auth in checked):
            raise RuntimeError('PROVIDER_AUTH_RECOVERY_REQUIRED')
        return checked
    finally:
        for fd in locks:
            os.close(fd)


def inherited_auth_lock(auth, fd):
    auth = private_path(auth)
    info = os.fstat(fd)
    path = private_path(Path(str(auth) + '.lock')).stat()
    if fd < 3 or (info.st_dev, info.st_ino) != (path.st_dev, path.st_ino):
        raise RuntimeError('AUTH_LEASE_DESCRIPTOR_INVALID')
    os.set_inheritable(fd, True)
    return fd


def health_path(auth):
    return Path(str(auth) + '.health.json')


def _health(auth):
    path = health_path(auth)
    if not path.exists():
        return None
    try:
        value = json.loads(private_path(path).read_text())
        if value.get('state') not in ['active', 'quarantined']:
            raise ValueError()
        return value
    except (OSError, ValueError, AttributeError):
        raise RuntimeError('PROVIDER_AUTH_HEALTH_INVALID') from None


def recover(auth, *, fd=None):
    owned = fd is None
    fd = auth_lock(auth, nonblocking=True) if owned else inherited_auth_lock(auth, fd)
    try:
        _auth(auth)
        health_path(auth).unlink(missing_ok=True)
    finally:
        if owned:
            os.close(fd)


def quarantine(auth, reason, *, fd=None):
    owned = fd is None
    fd = auth_lock(auth, nonblocking=True) if owned else inherited_auth_lock(auth, fd)
    try:
        if not isinstance(reason, str) or not reason or not reason.replace('_', '').isalnum():
            raise RuntimeError('PROVIDER_AUTH_HEALTH_REASON_INVALID')
        write_private(health_path(auth), {'state': 'quarantined', 'reason': reason})
    finally:
        if owned:
            os.close(fd)


def remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise RuntimeError('ATTEMPT_DEADLINE_EXCEEDED')
    return value


class Lease:
    def __init__(self, auth, fd, owner):
        self.path, self.fd = auth, fd
        self.secrets = [value for key, value in _auth(auth).items() if key in ['access', 'refresh', 'key']]
        self.owner = owner
        self.closed = False
        info = auth.stat()
        self.identity = (info.st_dev, info.st_ino)
        write_private(health_path(auth), {'state': 'active', 'owner': owner, 'pid': os.getpid(), 'started_at': time.time()})

    def quarantine(self, reason):
        write_private(health_path(self.path), {'state': 'quarantined', 'reason': reason, 'owner': self.owner})

    def current_secrets(self):
        info = private_path(self.path).stat()
        if (info.st_dev, info.st_ino) != self.identity:
            self.quarantine('PROVIDER_AUTH_INODE_CHANGED')
            raise RuntimeError('PROVIDER_AUTH_INODE_CHANGED')
        try:
            value = _auth(self.path)
        except RuntimeError:
            self.quarantine('PROVIDER_AUTH_INVALID')
            raise
        return [value[key] for key in ['access', 'refresh']]

    def close(self, *, completed=False):
        if self.closed:
            return
        try:
            state = _health(self.path)
            if completed and state and state.get('state') == 'active':
                health_path(self.path).unlink()
            elif state and state.get('state') == 'active':
                self.quarantine('PROVIDER_AUTH_INTERRUPTED')
        finally:
            self.closed = True
            os.close(self.fd)


def acquire(paths, *, deadline, owner, expected_count=8, cancelled=None):
    paths = validate_catalog(paths, expected_count=expected_count, inspect_auth=False, allow_unavailable=True)
    while True:
        if cancelled and cancelled():
            raise RuntimeError('AUTH_WAIT_CANCELLED')
        remaining(deadline)
        busy = False
        for auth in paths:
            try:
                fd = _slot_lock(auth)
            except FileNotFoundError:
                continue
            except RuntimeError as error:
                if str(error) == 'AUTH_PERMISSIONS_INVALID':
                    continue
                if str(error) != 'AUTH_BUSY':
                    raise
                busy = True
                continue
            try:
                try:
                    state = _health(auth)
                except RuntimeError:
                    write_private(health_path(auth), {'state': 'quarantined', 'reason': 'PROVIDER_AUTH_HEALTH_INVALID'})
                    os.close(fd)
                    continue
                if state:
                    if state['state'] == 'active':
                        write_private(health_path(auth), {'state': 'quarantined', 'reason': 'PROVIDER_AUTH_INTERRUPTED', 'owner': state.get('owner')})
                    os.close(fd)
                    continue
                try:
                    value = _auth(auth)
                except RuntimeError:
                    write_private(health_path(auth), {'state': 'quarantined', 'reason': 'PROVIDER_AUTH_INVALID'})
                    os.close(fd)
                    continue
                # Manual login/repair also holds this lock; busy refreshes are not read.
                for other in paths:
                    if other == auth:
                        continue
                    try:
                        other_fd = _slot_lock(other)
                    except FileNotFoundError:
                        continue
                    except RuntimeError as error:
                        if str(error) not in ['AUTH_BUSY', 'AUTH_PERMISSIONS_INVALID']:
                            raise
                        continue
                    try:
                        try:
                            other_value = _auth(other)
                        except RuntimeError:
                            continue
                        user = user_identity(value)
                        if (value['refresh'] == other_value['refresh'] or value['access'] == other_value['access'] or
                                (user is not None and user == user_identity(other_value))):
                            raise RuntimeError('PROVIDER_AUTH_DUPLICATE')
                    finally:
                        os.close(other_fd)
                return Lease(auth, fd, owner)
            except BaseException:
                os.close(fd)
                raise
        if not busy:
            raise RuntimeError('PROVIDER_AUTH_UNAVAILABLE')
        time.sleep(min(0.1, remaining(deadline)))


def _slot_lock(auth):
    # The permanent lock survives deletion/corruption of its credential file.
    private_path(auth.parent, directory=True)
    private_path(auth.parent.parent, directory=True)
    path = Path(str(auth) + '.lock')
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(fd).st_nlink != 1:
            raise RuntimeError('AUTH_PATH_INVALID')
        private_path(path)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.set_inheritable(fd, True)
        return fd
    except BlockingIOError:
        os.close(fd)
        raise RuntimeError('AUTH_BUSY') from None
    except BaseException:
        os.close(fd)
        raise


def account_lock_path(config, config_path=None):
    url = urlsplit(config['loginom']['url'])
    identity = urlunsplit((url.scheme.lower(), url.netloc.lower(), url.path.rstrip('/'), '', '')) + '\n' + config['loginom']['username']
    base = config.get('loginom_lock_dir')
    if not base:
        base = Path(config.get('operator_file', config_path or '')).parent / 'account-locks'
    base = Path(base)
    if not base.is_absolute() or base.resolve() != base:
        raise RuntimeError('LOGINOM_ACCOUNT_LOCK_PATH_INVALID')
    base.mkdir(mode=0o700, exist_ok=True)
    private_path(base, directory=True)
    return base / (hashlib.sha256(identity.encode()).hexdigest() + '.lock')


def inherited_account_lock(config, fd, config_path=None):
    path = private_path(account_lock_path(config, config_path))
    info = os.fstat(fd)
    if fd < 3 or (info.st_dev, info.st_ino) != (path.stat().st_dev, path.stat().st_ino):
        raise RuntimeError('LOGINOM_ACCOUNT_LEASE_DESCRIPTOR_INVALID')
    os.set_inheritable(fd, True)
    return fd


def loginom_lock(config, *, deadline, config_path=None, cancelled=None):
    path = account_lock_path(config, config_path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        private_path(path)
        while True:
            if cancelled and cancelled():
                raise RuntimeError('ACCOUNT_WAIT_CANCELLED')
            remaining(deadline)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.set_inheritable(fd, True)
                return fd
            except BlockingIOError:
                time.sleep(min(0.1, remaining(deadline)))
    except BaseException:
        os.close(fd)
        raise
