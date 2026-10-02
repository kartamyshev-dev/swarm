#!/usr/bin/env python3
"""Forward selected Multica events; Telegram updates belong to Multica."""
import argparse
import json
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def safe_text(value):
    text = re.sub(r'\[([^\]]+)\]\(mention://[^)]+\)', r'\1', str(value or ''))
    text = re.sub(r'\b\d{8,12}:[A-Za-z0-9_-]{25,}\b', '[секрет скрыт]', text)
    text = re.sub(r'\b(?:mul_|mat_)[A-Za-z0-9_-]+', '[секрет скрыт]', text)
    text = re.sub(r'(?i)\b(?:Bearer|Basic)\s+[^\s,;]+', '[секрет скрыт]', text)
    text = re.sub(r'(?im)((?:password|пароль|token|токен|api[_ -]?key|authorization)\s*[:=]\s*)[^\s,;]+', r'\1[скрыто]', text)
    return text


def question_event(item, config):
    return (item.get('type') == 'mentioned'
            and item.get('recipient_id') == config['owner_id']
            and item.get('actor_type') == 'agent'
            and item.get('actor_id') in config['agent_ids'])


class Notifier:
    def __init__(self, config, credentials, state, health='/tmp/notifier-health.json'):
        self.config_path = Path(config)
        self.credentials_path = Path(credentials)
        self.health = Path(health)
        self.db = sqlite3.connect(state)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS issue_state (
                id TEXT PRIMARY KEY, status TEXT NOT NULL, seen REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS event (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, issue_id TEXT NOT NULL,
                comment_id TEXT, created REAL NOT NULL, delivered INTEGER NOT NULL DEFAULT 0
            );
        ''')
        self.reload()

    def reload(self):
        self.config = json.loads(self.config_path.read_text())
        self.credentials = json.loads(self.credentials_path.read_text())

    def api(self, path):
        request = urllib.request.Request(self.config['api_url'].rstrip('/') + '/' + path,
            headers={'Authorization': 'Bearer ' + self.credentials['api_token'],
                     'X-Workspace-ID': self.config['workspace_id']})
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)

    def telegram(self, text, url):
        payload = {'chat_id': self.credentials['chat_id'], 'text': text[:3900],
                   'link_preview_options': {'is_disabled': True},
                   'reply_markup': {'inline_keyboard': [[{'text': 'Открыть карточку', 'url': url}]]}}
        request = urllib.request.Request(
            'https://api.telegram.org/bot' + self.credentials['bot_token'] + '/sendMessage',
            data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.load(response)
        if not result.get('ok'):
            raise RuntimeError('Telegram delivery rejected')
        return result['result']['message_id']

    def in_scope(self, issue):
        return ((issue.get('assignee_type') == 'squad'
                 and issue.get('assignee_id') == self.config['squad_id'])
                or (issue.get('assignee_type') == 'agent'
                    and issue.get('assignee_id') in self.config['agent_ids']))

    def issues(self):
        result, offset = {}, 0
        assignees = [self.config['squad_id']] + self.config['agent_ids']
        while True:
            query = urllib.parse.urlencode({'assignee_ids': ','.join(assignees),
                                            'limit': 100, 'offset': offset})
            page = self.api('issues?' + query)
            rows = page['issues']
            result.update((x['id'], x) for x in rows if self.in_scope(x))
            offset += len(rows)
            if offset >= page['total'] or not rows:
                return result

    def queue(self, event_id, kind, issue_id, comment_id=None, created=None):
        self.db.execute('INSERT OR IGNORE INTO event(id,kind,issue_id,comment_id,created) VALUES(?,?,?,?,?)',
                        (event_id, kind, issue_id, comment_id, created or time.time()))

    def collect(self, issues, inbox):
        now = time.time()
        for issue_id, issue in issues.items():
            previous = self.db.execute('SELECT status FROM issue_state WHERE id=?', (issue_id,)).fetchone()
            status = issue['status']
            if status == 'blocked' and ((previous and previous[0] != status)
                                       or (not previous and timestamp(issue['created_at']) >= self.config['enabled_at'])):
                revision = issue.get('revision', issue['updated_at'])
                self.queue('blocked:' + issue_id + ':' + str(revision), 'blocked', issue_id)
            if not previous or previous[0] != status:
                self.db.execute('INSERT INTO issue_state VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,seen=excluded.seen',
                                (issue_id, status, now))
        cutoff = max(self.config['enabled_at'], now - 30 * 86400)
        for item in inbox:
            if item.get('workspace_id') != self.config['workspace_id'] or item.get('recipient_id') != self.config['owner_id']:
                continue
            created = timestamp(item['created_at'])
            if created < cutoff or item.get('issue_id') not in issues:
                continue
            if question_event(item, self.config):
                details = item.get('details') or {}
                self.queue(item['id'], 'question', item['issue_id'], details.get('comment_id'), created)
            elif item.get('type') == 'task_failed' and item.get('actor_id') in self.config['agent_ids']:
                self.queue(item['id'], 'failed', item['issue_id'], created=created)
        self.db.execute('DELETE FROM event WHERE created < ?', (cutoff,))
        for stale_id, in self.db.execute('SELECT id FROM issue_state WHERE seen < ?', (now - 30 * 86400,)).fetchall():
            if stale_id not in issues:
                self.db.execute('DELETE FROM issue_state WHERE id=?', (stale_id,))
        self.db.commit()

    def render(self, kind, issue, comment_id):
        headings = {'blocked': '⛔ Задача заблокирована', 'question': '❓ Нужен ваш ответ',
                    'failed': '⚠️ Ошибка запуска агента'}
        url = (self.config['app_url'].rstrip('/') + '/' + self.config['workspace_slug']
               + '/issues/' + urllib.parse.quote(issue['id'], safe=''))
        lines = [headings[kind], safe_text(issue['identifier'] + ' — ' + issue['title'])[:500]]
        if kind in ('blocked', 'question'):
            comments = self.api('issues/' + issue['id'] + '/comments')
            relevant = [x for x in comments if x.get('author_type') == 'agent'
                        and x.get('author_id') in self.config['agent_ids']]
            selected = next((x for x in relevant if x['id'] == comment_id), None)
            if not selected and kind == 'blocked' and relevant:
                selected = max(relevant, key=lambda x: x['created_at'])
            if selected:
                lines.append(safe_text(selected['content'])[:2400])
        lines.append('Ответьте в карточке Multica.' if kind == 'question' else 'Подробности — в карточке Multica.')
        return '\n\n'.join(lines), url

    def deliver(self, issues):
        if not self.credentials.get('chat_id'):
            return 0
        count = 0
        for event_id, kind, issue_id, comment_id in self.db.execute(
                'SELECT id,kind,issue_id,comment_id FROM event WHERE delivered=0 ORDER BY created').fetchall():
            issue = issues.get(issue_id)
            obsolete = not issue or issue['status'] in ('done', 'cancelled', 'closed')
            obsolete = obsolete or (kind == 'blocked' and issue['status'] != 'blocked')
            if not obsolete:
                text, url = self.render(kind, issue, comment_id)
                self.telegram(text, url)
                count += 1
                print(json.dumps({'event': 'delivered', 'kind': kind, 'issue_id': issue_id}), flush=True)
            self.db.execute('UPDATE event SET delivered=1 WHERE id=?', (event_id,))
            self.db.commit()
        return count

    def once(self):
        self.reload()
        issues = self.issues()
        self.collect(issues, self.api('inbox'))
        delivered = self.deliver(issues)
        self.health.write_text(json.dumps({'last_success': time.time(),
                                          'target_configured': bool(self.credentials.get('chat_id'))}))
        return delivered


def timestamp(value):
    from datetime import datetime
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='/app/config.json')
    parser.add_argument('--credentials', default='/app/credentials.json')
    parser.add_argument('--state', default='/state/notifier.sqlite')
    parser.add_argument('--health', action='store_true')
    args = parser.parse_args()
    if args.health:
        info = json.loads(Path('/tmp/notifier-health.json').read_text())
        raise SystemExit(0 if time.time() - info['last_success'] < 180 else 1)
    app = Notifier(args.config, args.credentials, args.state)
    while True:
        try:
            app.once()
        except Exception as exc:
            # Exception messages from HTTP libraries can contain credential URLs.
            print(json.dumps({'event': 'poll_failed', 'error_type': type(exc).__name__,
                              'http_status': getattr(exc, 'code', None)}), flush=True)
        time.sleep(app.config.get('poll_seconds', 30))


if __name__ == '__main__':
    main()
