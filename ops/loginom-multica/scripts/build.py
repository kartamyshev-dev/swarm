#!/usr/bin/env python3
"""Build one verified Linux CLI in a daemon-owned checkout; no retention GC."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import signal
from common import artifact_lock, checked_path, managed_root, ops_identity, verify_candidate, write_private


def build_command(command, cwd, lock, timeout=900):
    process = subprocess.Popen(command, cwd=cwd, pass_fds=(lock,), start_new_session=True)
    try:
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError('BUILD_COMMAND_TIMED_OUT') from None
        if code:
            raise RuntimeError('BUILD_COMMAND_FAILED')
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()


def publish(staged, current, temporary):
    previous = temporary / 'previous'
    try:
        if current.exists():
            current.rename(previous)
        staged.rename(current)
    except BaseException:
        if previous.exists() and not current.exists():
            previous.rename(current)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('worktree', type=Path)
    parser.add_argument('out', type=Path)
    args = parser.parse_args()
    if os.uname().sysname != 'Linux' or os.uname().machine != 'x86_64':
        raise RuntimeError('LINUX_X64_REQUIRED')
    root, owner = managed_root(args.worktree)
    if args.out != root / 'current':
        raise RuntimeError('OUT_MUST_BE_MANAGED_CURRENT')
    current = checked_path(args.out, root)
    if current.exists():
        marker = current / 'cli-manifest.json'
        if not marker.is_file() or marker.is_symlink() or json.loads(marker.read_text()).get('format') != 'loginom-cli-artifact-v1':
            raise RuntimeError('CURRENT_OWNER_UNKNOWN')
        for path in current.rglob('*'):
            if path.is_symlink() and not path.resolve().is_relative_to(current):
                raise RuntimeError('CURRENT_EXTERNAL_LINK')
    lock = artifact_lock(root)
    try:
        if subprocess.check_output(['bun', '--version'], text=True).strip() != '1.3.14':
            raise RuntimeError('PINNED_BUN_REQUIRED')
        tools = Path.home() / '.local/share/loginom-multica-tools'
        os.environ.setdefault('LOGINOM_AI_AGENT_NODE_SOURCE', str(tools / 'node-v24.19.0-linux-x64/bin/node'))
        os.environ.setdefault('LOGINOM_AI_AGENT_BROWSER_SOURCE', str(tools / 'browsers'))
        if current.exists() and verify_candidate(args.worktree, current).returncode == 0:
            print(json.dumps({'artifact': str(current), 'reused': True, 'ops': ops_identity()}))
            return
        build_command(['bun', 'install', '--frozen-lockfile', '--ignore-scripts', '--linker=hoisted', '--filter', '@loginom-ai-agent/agent', '--filter', '@loginom-ai-agent/loginom-host'], args.worktree, lock)
        staging = checked_path(root / 'staging', root)
        staging.mkdir(exist_ok=True, mode=0o700)
        temporary = Path(tempfile.mkdtemp(prefix='build-', dir=staging))
        try:
            staged = temporary / 'payload'
            build_command(['bun', 'run', 'script/build-cli.ts', str(staged), '--no-archive'], args.worktree / 'packages/loginom-host', lock)
            if verify_candidate(args.worktree, staged).returncode:
                raise RuntimeError('NEW_CANDIDATE_INVALID')
            write_private(root / 'build.json', {'owner': owner, 'ops': ops_identity()})
            publish(staged, current, temporary)
            print(json.dumps({'artifact': str(current), 'reused': False, 'ops': ops_identity()}))
        finally:
            # Only this invocation's temporary output is removed. SIGKILL leftovers belong to native GC.
            shutil.rmtree(temporary)
    finally:
        os.close(lock)


if __name__ == '__main__':
    def interrupted(signum, frame):
        raise RuntimeError('BUILD_INTERRUPTED')
    signal.signal(signal.SIGTERM, interrupted)
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(str(error) if isinstance(error, RuntimeError) else 'BUILD_COMMAND_FAILED', file=__import__('sys').stderr)
        raise SystemExit(1)
