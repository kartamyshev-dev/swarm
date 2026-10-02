#!/usr/bin/env python3
"""Export only tracked operator files from a committed revision, with an inventory."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument('--repo', required=True, type=Path)
parser.add_argument('--ref', required=True)
parser.add_argument('--out', required=True, type=Path)
args = parser.parse_args()
commit = subprocess.check_output(['git', '-C', str(args.repo), 'rev-parse', args.ref + '^{commit}'], text=True).strip()
if args.out.exists():
    raise SystemExit('NEW_OUTPUT_REQUIRED')
args.out.mkdir(parents=True, mode=0o700)
prefix = 'ops/loginom-multica/'
files = {}
names = subprocess.check_output(['git', '-C', str(args.repo), 'ls-tree', '-r', '--name-only', commit, '--', prefix], text=True).splitlines()
for name in names:
    relative = name.removeprefix(prefix)
    if not relative.startswith(('scripts/', 'instructions/')) and relative not in ['README.md', 'deployment.example.json']:
        continue
    body = subprocess.check_output(['git', '-C', str(args.repo), 'show', commit + ':' + name])
    path = args.out / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    path.chmod(0o700 if relative.startswith('scripts/') else 0o600)
    files[relative] = hashlib.sha256(body).hexdigest()
digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
(args.out / 'VERSION.json').write_text(json.dumps({'commit': commit, 'bundle_sha256': digest, 'files': files}, indent=2) + '\n')
print(json.dumps({'commit': commit, 'bundle_sha256': digest, 'files': len(files)}))
