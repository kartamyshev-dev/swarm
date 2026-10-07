#!/usr/bin/env python3
"""Keep one Loginom identity owned until a foreground command and its children exit."""
import argparse
import os
import signal
import subprocess
import time
from pathlib import Path
from common import read_private
from provider_pool import inherited_account_lock, loginom_lock


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--timeout', type=float, default=7200)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command or args.timeout <= 0:
        raise RuntimeError('COMMAND_AND_POSITIVE_TIMEOUT_REQUIRED')
    config = read_private(args.config)
    deadline = time.monotonic() + args.timeout
    borrowed = os.environ.get('LOGINOM_MULTICA_ACCOUNT_FD')
    fd = inherited_account_lock(config, int(borrowed), config_path=args.config) if borrowed else loginom_lock(config, deadline=deadline, config_path=args.config)
    process = None
    previous = {}
    def interrupt(signum, frame):
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signum)
        raise RuntimeError('ACCOUNT_COMMAND_CANCELLED')
    try:
        for signum in [signal.SIGTERM, signal.SIGINT]:
            previous[signum] = signal.signal(signum, interrupt)
        env = {**os.environ, 'LOGINOM_MULTICA_ACCOUNT_FD': str(fd)}
        process = subprocess.Popen(command, env=env, pass_fds=(fd,), start_new_session=True)
        try:
            return process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise RuntimeError('ACCOUNT_COMMAND_TIMED_OUT') from None
    finally:
        # Signal the entire owned group, including children of an already-exited parent.
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            if process.poll() is None:
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        if not borrowed:
            os.close(fd)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as error:
        print(str(error) if isinstance(error, RuntimeError) else 'ACCOUNT_DESCRIPTOR_INVALID', file=__import__('sys').stderr)
        raise SystemExit(1)
