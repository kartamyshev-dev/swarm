import json
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from notifier import Notifier, question_event, safe_text


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


class TestNotifier(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = time.time()
        config = {'workspace_id': 'ws', 'owner_id': 'owner', 'squad_id': 'squad',
                  'agent_ids': ['leader', 'worker'], 'enabled_at': self.now-10,
                  'app_url': 'https://mas.example', 'workspace_slug': 'lab'}
        (self.root/'config.json').write_text(json.dumps(config))
        (self.root/'credentials.json').write_text('{"chat_id":"123"}')
        self.app = Notifier(self.root/'config.json', self.root/'credentials.json', self.root/'db')
        self.sent = []
        self.app.telegram = lambda text, url: self.sent.append((text, url))
        self.app.api = lambda path: [{'id':'comment', 'author_type':'agent', 'author_id':'leader',
                                     'created_at':iso(self.now), 'content':'Какое поле использовать?'}]
        self.issue = {'id':'issue', 'identifier':'LAB-1', 'title':'Узел', 'status':'in_progress',
                      'created_at':iso(self.now-1000), 'updated_at':iso(self.now), 'revision':1,
                      'assignee_type':'squad', 'assignee_id':'squad'}
        self.issues = {'issue':self.issue}
        self.mention = {'id':'notice', 'type':'mentioned', 'recipient_id':'owner',
                        'workspace_id':'ws', 'actor_type':'agent', 'actor_id':'leader',
                        'issue_id':'issue', 'created_at':iso(self.now), 'details':{'comment_id':'comment'}}

    def tearDown(self):
        self.app.db.close()
        self.tmp.cleanup()

    def test_blocked_baseline_transition_and_resolution(self):
        self.issue['status']='blocked'
        self.app.collect(self.issues, [])
        self.assertEqual(self.app.deliver(self.issues), 0)
        self.issue['status']='in_progress'
        self.app.collect(self.issues, [])
        self.issue.update(status='blocked', revision=2)
        self.app.collect(self.issues, [])
        self.assertEqual(self.app.deliver(self.issues), 1)
        self.app.collect(self.issues, [])
        self.assertEqual(self.app.deliver(self.issues), 0)
        self.issue['status']='in_progress'
        self.app.collect(self.issues, [])
        self.issue.update(status='blocked', revision=3)
        self.app.collect(self.issues, [])
        self.issue['status']='done'
        self.assertEqual(self.app.deliver(self.issues), 0)

    def test_owner_question_and_filters(self):
        self.assertFalse(question_event(dict(self.mention, recipient_id='worker'), self.app.config))
        self.app.collect(self.issues, [dict(self.mention, created_at=iso(self.now-100)),
                                      dict(self.mention, id='foreign', recipient_id='another')])
        self.assertEqual(self.app.deliver(self.issues), 0)
        self.app.collect(self.issues, [self.mention])
        self.assertEqual(self.app.deliver(self.issues), 1)
        self.assertIn('Какое поле', self.sent[0][0])
        self.assertEqual(self.sent[0][1], 'https://mas.example/lab/issues/issue')
        self.assertFalse(self.app.in_scope(dict(self.issue, assignee_id='someone')))

    def test_missing_target_failed_send_retry_and_restart(self):
        self.app.collect(self.issues, [self.mention])
        self.app.credentials.clear()
        self.assertEqual(self.app.deliver(self.issues), 0)
        self.app.credentials['chat_id']='123'
        def fail(*args):
            raise TimeoutError('unknown delivery')
        self.app.telegram=fail
        with self.assertRaises(TimeoutError):
            self.app.deliver(self.issues)
        self.app.db.close()
        self.app.db=sqlite3.connect(self.root/'db')
        self.app.telegram=lambda text,url:self.sent.append((text,url))
        self.assertEqual(self.app.deliver(self.issues), 1)
        self.app.collect(self.issues, [self.mention])
        self.assertEqual(self.app.deliver(self.issues), 0)

    def test_failure_event_and_redaction(self):
        self.app.collect(self.issues, [dict(self.mention, type='task_failed')])
        self.assertEqual(self.app.deliver(self.issues), 1)
        self.assertIn('Ошибка запуска', self.sent[0][0])
        value=safe_text('[@Лидер](mention://agent/id) token=private\nAuthorization: Bearer abcdef\n'
                        '1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZ123456789 mul_private')
        for secret in ['private','abcdef','ABCDEFGHIJKLMNOPQRSTUVWXYZ','mul_']:
            self.assertNotIn(secret,value)
        self.assertIn('@Лидер',value)

    def test_paginated_squad_cards(self):
        paths=[]
        def api(path):
            paths.append(path)
            return {'issues':[dict(self.issue,id=str(len(paths)))],'total':2}
        self.app.api=api
        self.assertEqual(set(self.app.issues()), {'1','2'})
        self.assertIn('offset=1', paths[1])


if __name__ == '__main__':
    unittest.main()
