#!/usr/bin/env python3
"""Configure native MCP and submit a short project note through OpenViking's session API."""
import argparse
import hashlib
import json
from pathlib import Path
import urllib.error
import urllib.request
import uuid

from common import read_private, write_private

PEER = '-Users-kartamyshev-Git-loginom-ai-agent'
URL = 'https://ov.kartamyshev.dev'
DEFAULT_CONFIG = Path.home() / '.config/loginom-multica/openviking.json'


def mcp_server(config):
    if config.get('peer_id') != PEER or config.get('url', '').rstrip('/') != URL:
        raise RuntimeError('OPENVIKING_PROJECT_CONFIG_MISMATCH')
    if not isinstance(config.get('api_key'), str) or not config['api_key'].strip():
        raise RuntimeError('OPENVIKING_API_KEY_REQUIRED')
    headers = {
        'Authorization': 'Bearer ' + config['api_key'],
        'X-OpenViking-Actor-Peer': PEER,
    }
    for field, header in [('account', 'X-OpenViking-Account'), ('user', 'X-OpenViking-User')]:
        if config.get(field):
            headers[header] = config[field]
    return {'type': 'http', 'url': URL + '/mcp', 'headers': headers}


def request(config, path, data=None):
    headers = {**mcp_server(config)['headers'], 'Content-Type': 'application/json'}
    req = urllib.request.Request(URL + '/api/v1/' + path,
        data=json.dumps(data).encode() if data is not None else None, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            body = json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError('OPENVIKING_HTTP_' + str(error.code)) from None
    except urllib.error.URLError:
        raise RuntimeError('OPENVIKING_UNAVAILABLE') from None
    if body.get('status') != 'ok':
        raise RuntimeError('OPENVIKING_REQUEST_FAILED')
    return body['result']


def save_note(config, content, receipt_path):
    mcp_server(config)
    if not content.strip() or len(content) > 8000:
        raise RuntimeError('SHORT_MEMORY_NOTE_REQUIRED')
    receipt_path = Path(receipt_path)
    if receipt_path.exists():
        raise RuntimeError('NEW_MEMORY_RECEIPT_REQUIRED')
    session = 'multica-note-' + uuid.uuid4().hex
    receipt = {'session_id': session, 'peer_id': PEER,
               'content_sha256': hashlib.sha256(content.encode()).hexdigest(), 'status': 'prepared'}
    # Keep the session identity before a possibly ambiguous network failure; never retry silently.
    write_private(receipt_path, receipt)
    request(config, 'sessions/' + session + '/messages',
            {'role': 'user', 'peer_id': PEER, 'content': content})
    receipt['status'] = 'message_saved'
    write_private(receipt_path, receipt)
    committed = request(config, 'sessions/' + session + '/commit', {})
    receipt.update(status='submitted', task_id=committed.get('task_id'),
                   archive_uri=committed.get('archive_uri'))
    write_private(receipt_path, receipt)
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--file', required=True, type=Path)
    parser.add_argument('--receipt', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(save_note(read_private(args.config), args.file.read_text(), args.receipt)))
