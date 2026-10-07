#!/usr/bin/env python3
"""Install one verified ops bundle during the exact native maintenance run."""
import sys
sys.dont_write_bytecode = True

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
from api import API
from common import read_private
from concurrency import ensure_idle

ROOT_FILES = {'README.md', 'PARALLEL.md', 'deployment.example.json', 'operator.example.json', 'openviking.example.json'}
LOCAL_PATCH_FIELDS = {'issue', 'approval_comment', 'instruction_comment', 'base_bundle_sha256', 'patch_sha256', 'scope'}


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise RuntimeError('BUNDLE_DUPLICATE_KEY')
        result[key] = value
    return result


def root_path(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts or path.resolve() != path:
        raise RuntimeError('BUNDLE_ROOT_INVALID')
    return path


def inspect_tree(root, names=None):
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError('BUNDLE_ROOT_INVALID')
    files, directories = set(), set()
    for path in root.rglob('*'):
        info = path.lstat()
        if info.st_uid != os.getuid() or stat.S_ISLNK(info.st_mode):
            raise RuntimeError('BUNDLE_LINK_OR_OWNER_INVALID')
        relative = path.relative_to(root).as_posix()
        if stat.S_ISDIR(info.st_mode):
            directories.add(relative)
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            files.add(relative)
        else:
            raise RuntimeError('BUNDLE_FILE_INVALID')
    if names is not None:
        expected_dirs = {str(parent) for name in names for parent in PurePosixPath(name).parents
                         if str(parent) != '.'}
        if files != set(names) or directories != expected_dirs:
            raise RuntimeError('BUNDLE_INVENTORY_MISMATCH')
    return files


def verify_bundle(root, commit=None, digest=None, *, exact=True):
    root = root_path(root)
    inspect_tree(root)
    try:
        version = json.loads((root / 'VERSION.json').read_text(), object_pairs_hook=pairs)
    except (OSError, ValueError):
        raise RuntimeError('BUNDLE_VERSION_INVALID') from None
    keys = set(version) if isinstance(version, dict) else None
    allowed_keys = ({'commit', 'bundle_sha256', 'files'},) if exact else (
        {'commit', 'bundle_sha256', 'files'}, {'commit', 'bundle_sha256', 'files', 'local_patch'})
    if (keys not in allowed_keys or
            not isinstance(version['commit'], str) or not re.fullmatch('[0-9a-f]{40}', version['commit']) or
            not isinstance(version['bundle_sha256'], str) or not re.fullmatch('[0-9a-f]{64}', version['bundle_sha256']) or
            not isinstance(version['files'], dict) or not version['files']):
        raise RuntimeError('BUNDLE_VERSION_INVALID')
    if 'local_patch' in version:
        # Previous runtime provenance is preserved; file identity still comes from hashes.
        patch = version['local_patch']
        if (not isinstance(patch, dict) or set(patch) != LOCAL_PATCH_FIELDS or
                any(not isinstance(patch[key], str) or
                    not re.fullmatch('[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}', patch[key])
                    for key in ['issue', 'approval_comment', 'instruction_comment']) or
                any(not isinstance(patch[key], str) or not re.fullmatch('[0-9a-f]{64}', patch[key])
                    for key in ['base_bundle_sha256', 'patch_sha256']) or
                not isinstance(patch['scope'], str) or not patch['scope'].strip()):
            raise RuntimeError('BUNDLE_LOCAL_PATCH_INVALID')
    for name, checksum in version['files'].items():
        relative = PurePosixPath(name)
        if (not isinstance(name, str) or relative.is_absolute() or '..' in relative.parts or
                str(relative) != name or '\\' in name or
                not (name.startswith(('scripts/', 'instructions/')) or name in ROOT_FILES) or
                not isinstance(checksum, str) or not re.fullmatch('[0-9a-f]{64}', checksum)):
            raise RuntimeError('BUNDLE_INVENTORY_INVALID')
    expected = hashlib.sha256(json.dumps(version['files'], sort_keys=True).encode()).hexdigest()
    if version['bundle_sha256'] != expected or (commit is not None and version['commit'] != commit) or (digest is not None and expected != digest):
        raise RuntimeError('BUNDLE_IDENTITY_MISMATCH')
    if exact:
        inspect_tree(root, [*version['files'], 'VERSION.json'])
    for name, checksum in version['files'].items():
        path = root / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
            raise RuntimeError('BUNDLE_FILE_HASH_MISMATCH')
    return version


def maintenance(api, deployment):
    task_id, agent_id = os.environ.get('MULTICA_TASK_ID'), os.environ.get('MULTICA_AGENT_ID')
    if (not task_id or not deployment.get('maintenance_issue_id') or
            agent_id not in deployment['agents'].values() or
            os.environ.get('MULTICA_WORKSPACE_ID') != deployment['workspace_id']):
        raise RuntimeError('NATIVE_MAINTENANCE_REQUIRED')
    ensure_idle(api, deployment)
    active = [task for task in api.request('agent-task-snapshot') if task.get('status') not in {'completed', 'failed', 'cancelled'}]
    if (len(active) != 1 or active[0].get('id') != task_id or active[0].get('status') != 'running' or
            active[0].get('workspace_id') != deployment['workspace_id'] or
            active[0].get('issue_id') != deployment['maintenance_issue_id'] or active[0].get('agent_id') != agent_id):
        raise RuntimeError('NATIVE_MAINTENANCE_REQUIRED')


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def install(api, deployment, bundle, target, commit, digest):
    if not re.fullmatch('[0-9a-f]{40}', commit) or not re.fullmatch('[0-9a-f]{64}', digest):
        raise RuntimeError('BUNDLE_EXPECTED_IDENTITY_INVALID')
    bundle, target = root_path(bundle), root_path(target)
    if (bundle == target or bundle.is_relative_to(target) or target.is_relative_to(bundle) or
            any((parent / '.managed_env.json').exists() for parent in [target, *target.parents])):
        raise RuntimeError('BUNDLE_INSTALL_TARGET_INVALID')
    version = verify_bundle(bundle, commit, digest)
    maintenance(api, deployment)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.parent.stat().st_uid != os.getuid() or target.parent.stat().st_mode & 0o022:
        raise RuntimeError('BUNDLE_INSTALL_PARENT_INVALID')
    lock = os.open(target.parent / ('.' + target.name + '-install.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    staging, backup = None, None
    moved, installed = False, False
    try:
        info = os.fstat(lock)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or
                stat.S_IMODE(info.st_mode) != 0o600):
            raise RuntimeError('BUNDLE_INSTALL_LOCK_INVALID')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('BUNDLE_INSTALL_BUSY') from None
        # Preserve the entire previous verified version, including generated caches.
        previous = verify_bundle(target, exact=False) if target.exists() else None
        staging = Path(tempfile.mkdtemp(prefix='.' + target.name + '-staging-', dir=target.parent))
        for name in [*version['files'], 'VERSION.json']:
            path = staging / name
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with path.open('xb') as stream:
                stream.write((bundle / name).read_bytes())
                stream.flush()
                os.fsync(stream.fileno())
            path.chmod(0o700 if name.startswith('scripts/') else 0o600)
        verify_bundle(staging, commit, digest)
        for directory in sorted([path for path in staging.rglob('*') if path.is_dir()], reverse=True):
            sync_directory(directory)
        sync_directory(staging)
        # Refuse peer work that appeared while staging the payload.
        maintenance(api, deployment)
        if previous is not None:
            verify_bundle(target, previous['commit'], previous['bundle_sha256'], exact=False)
            backup = Path(tempfile.mkdtemp(prefix='.' + target.name + '-previous-' + previous['commit'][:12] + '-', dir=target.parent))
            backup.rmdir()
            os.replace(target, backup)
            backup.chmod(0o700)
            moved = True
        os.replace(staging, target)
        installed = True
        sync_directory(target.parent)
        verify_bundle(target, commit, digest)
        return {'commit': commit, 'bundle_sha256': digest, 'files': len(version['files']),
                'installed': str(target), 'previous_bundle': str(backup) if backup else None,
                'readback': 'verified'}
    except Exception:
        if installed and target.exists():
            # Remove only the directory we created with an unchanged inventory.
            inspect_tree(target, [*version['files'], 'VERSION.json'])
            shutil.rmtree(target)
        if moved and backup.exists() and not target.exists():
            os.replace(backup, target)
            sync_directory(target.parent)
        raise
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)
        os.close(lock)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bundle', required=True, type=Path)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--digest', required=True)
    parser.add_argument('--deployment', required=True, type=Path)
    parser.add_argument('--target', type=Path, default=Path.home() / '.local/share/loginom-multica')
    args = parser.parse_args()
    result = install(API(), read_private(args.deployment), args.bundle, args.target, args.commit, args.digest)
    print(json.dumps(result))


if __name__ == '__main__':
    try:
        main()
    except RuntimeError as error:
        # Emit only controlled error codes, never private paths or API bodies.
        code = str(error)
        raise SystemExit(code if re.fullmatch('[A-Z0-9_]+', code) else 'BUNDLE_INSTALL_FAILED') from None
    except OSError:
        raise SystemExit('BUNDLE_INSTALL_IO_FAILED') from None
