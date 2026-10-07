#!/usr/bin/env python3
"""Explicit maintenance-time limits; never create, resume or mention an issue."""
import argparse
import json
import os
from pathlib import Path
from api import API
from common import write_private

TERMINAL = {'completed', 'failed', 'cancelled'}


def ensure_idle(api, deployment):
    if set(deployment['agents']) != {'generator', 'worker', 'reviewer'} or len(set(deployment['agents'].values())) != 3:
        raise RuntimeError('THREE_ROLES_REQUIRED')
    for id in deployment['agents'].values():
        if api.request('agents/' + id).get('id') != id:
            raise RuntimeError('AGENT_ACCESS_UNCONFIRMED')
    snapshot = api.request('agent-task-snapshot')
    if not isinstance(snapshot, list) or any(not isinstance(task, dict) for task in snapshot):
        raise RuntimeError('WORKSPACE_NOT_IDLE')
    active = [task for task in snapshot if task.get('status') not in TERMINAL]
    if not active:
        return
    # A native service run may install its own bundle, but cannot exempt peers.
    issue_id = deployment.get('maintenance_issue_id')
    task_id = os.environ.get('MULTICA_TASK_ID')
    agent_id = os.environ.get('MULTICA_AGENT_ID')
    if (len(active) != 1 or not issue_id or not task_id or
            os.environ.get('MULTICA_WORKSPACE_ID') != deployment['workspace_id']):
        raise RuntimeError('WORKSPACE_NOT_IDLE')
    task = active[0]
    if (task.get('id') != task_id or task.get('status') != 'running' or
            task.get('workspace_id') != deployment['workspace_id'] or
            task.get('issue_id') != issue_id or task.get('agent_id') != agent_id or
            agent_id not in deployment['agents'].values()):
        raise RuntimeError('WORKSPACE_NOT_IDLE')
    issue = api.request('issues/' + issue_id)
    if (issue.get('id') != issue_id or issue.get('workspace_id') != deployment['workspace_id'] or
            issue.get('assignee_type') != 'agent' or issue.get('assignee_id') != agent_id):
        raise RuntimeError('MAINTENANCE_IDENTITY_UNCONFIRMED')


def set_limits(api, deployment, parallel):
    if type(parallel) is not int or not 1 <= parallel <= 8:
        raise RuntimeError('CONCURRENCY_LIMIT_INVALID')
    limits = {'generator': 1, 'worker': parallel, 'reviewer': parallel}
    ensure_idle(api, deployment)
    for role, id in deployment['agents'].items():
        # Repeat before each update to refuse a queue that appeared during maintenance.
        ensure_idle(api, deployment)
        api.request('agents/' + id, {'max_concurrent_tasks': limits[role]})
        if api.request('agents/' + id).get('max_concurrent_tasks') != limits[role]:
            raise RuntimeError('CONCURRENCY_READBACK_MISMATCH')
    return limits


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--deployment', required=True, type=Path)
    parser.add_argument('--parallel', type=int, choices=range(1, 9), required=True)
    parser.add_argument('--confirm-idle', action='store_true', required=True,
                        help='No new cards, mentions, schedules or runs during this maintenance window')
    args = parser.parse_args()
    deployment = json.loads(args.deployment.read_text())
    if deployment['workspace_id'] != os.environ['MULTICA_WORKSPACE_ID']:
        raise RuntimeError('WORKSPACE_MISMATCH')
    limits = set_limits(API(), deployment, args.parallel)
    # Subsequent instruction reloads keep these explicit role limits.
    write_private(args.deployment, {**deployment, 'max_concurrent_tasks': limits})
    print(json.dumps({'max_concurrent_tasks': limits, 'readback': 'verified', 'runs_created': 0}))


if __name__ == '__main__':
    try:
        main()
    except RuntimeError as error:
        raise SystemExit(str(error))
