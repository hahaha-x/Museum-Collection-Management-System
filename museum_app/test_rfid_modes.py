"""仅使用临时 SQLite 和本机假服务，不连接 Reader，不修改 museum.db。"""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import app as web
from db import get_db
from rfid_source import RfidError, ensure_schema, source_state, validate_device, read_device


def real(epcs=None, obs_id='real-1', **overrides):
    obs = {'observation_id': obs_id, 'source': 'device', 'valid': True,
           'collected_at': datetime.now(timezone.utc).isoformat(),
           'epc_list': ['0011223344556691'] if epcs is None else epcs, 'error_message': None}
    obs.update(overrides)
    return obs


class ModeTests(unittest.TestCase):
    def setUp(self):
        # 固定在本测试文件所在目录内创建随机临时目录，避免受限 Windows 的系统 TEMP 权限问题。
        self.temp = tempfile.TemporaryDirectory(prefix='.rfid-test-', dir=Path(__file__).resolve().parent)
        self.path = str(Path(self.temp.name) / 'test.db')
        web.app.config.update(TESTING=True, DB_PATH=self.path, RFID_WORKER_ENABLED=False,
                              RFID_API_TOKEN='test-token-at-least-16', SECRET_KEY='test-only')
        conn = sqlite3.connect(self.path)
        conn.executescript(Path(__file__).with_name('schema_sqlite.sql').read_text(encoding='utf-8'))
        ensure_schema(conn)
        conn.execute("INSERT INTO users(id,employee_no,password_hash,name,role) VALUES(1,'a','unused','管理员','管理员')")
        conn.execute("INSERT INTO users(id,employee_no,password_hash,name,role) VALUES(2,'v','unused','查看者','查看者')")
        conn.execute("INSERT INTO rfid_devices(device_no,connected,collecting) VALUES('reader','在线',1)")
        conn.execute("INSERT INTO collections(collection_no,name,category) VALUES('A','测试藏品','陶瓷')")
        conn.execute("INSERT INTO inventory_status(collection_id,epc,status) VALUES(1,'0011223344556691','在库')")
        conn.execute("INSERT INTO rfid_observations(device_id,epc) VALUES(1,'OLD-MOCK')")
        conn.commit()
        conn.close()
        self.client = web.app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = 1
            session['rfid_csrf'] = 'test-csrf'

    def tearDown(self):
        self.temp.cleanup()

    def post(self, route, **data):
        return self.client.post(route, data=dict(csrf_token='test-csrf', **data))

    def query(self, sql):
        with web.app.app_context():
            return [dict(r) for r in get_db().execute(sql)]

    def poll(self):
        with web.app.app_context():
            web.rfid_poll_once()

    def device(self):
        self.post('/rfid/source', source='device')
        self.post('/rfid/toggle')

    def test_default_mock_preserved(self):
        with patch('app.read_device', side_effect=AssertionError('mock must not use network')):
            self.poll()
            self.assertEqual(self.client.get('/rfid/data').json['source'], 'mock')
            self.post('/rfid/simulate', mode='normal')
            self.assertTrue(self.client.get('/rfid/data').json['recent'])
            self.post('/rfid/simulate', mode='empty')
            data = self.client.get('/rfid/data').json
            self.assertTrue(data['valid'])
            self.assertEqual(data['recent'], [])
            self.post('/rfid/simulate', mode='fault')
            self.assertFalse(self.client.get('/rfid/data').json['valid'])

    def test_switch_persists_pauses_and_hides_old(self):
        self.poll()
        self.post('/rfid/source', source='device')
        data = self.client.get('/rfid/data').json
        self.assertEqual(data['source'], 'device')
        self.assertFalse(data['collecting'])
        self.assertEqual(data['recent'], [])
        self.assertTrue(self.query('SELECT * FROM rfid_observations'))
        self.assertTrue(self.query('SELECT * FROM collections'))
        self.assertEqual(self.query('SELECT mode FROM rfid_source_state')[0]['mode'], 'device')
        count = len(self.query('SELECT * FROM rfid_batches'))
        self.post('/rfid/simulate', mode='normal')
        self.assertEqual(len(self.query('SELECT * FROM rfid_batches')), count)
        self.post('/rfid/source', source='mock')
        self.assertEqual(self.client.get('/rfid/data').json['recent'], [])

    def test_real_empty_failure_recovery_no_fallback(self):
        self.device()
        with patch('app._do_read', side_effect=AssertionError('no fallback')):
            with patch('app.read_device', return_value=real()):
                self.poll()
                self.poll()  # 同一观测编号不重复入库
            self.assertEqual(len(self.query('SELECT * FROM rfid_batches')), 1)
            self.assertEqual(self.client.get('/rfid/data').json['recent'][0]['epc'], '0011223344556691')
            with patch('app.read_device', return_value=real([], 'empty')):
                self.poll()
            self.assertTrue(self.client.get('/rfid/data').json['valid'])
            self.assertEqual(self.client.get('/rfid/data').json['recent'], [])
            with patch('app.read_device', side_effect=RfidError('test disconnected')):
                self.poll()
            data = self.client.get('/rfid/data').json
            self.assertEqual(data['connected'], '故障')
            self.assertFalse(data['valid'])
            self.assertTrue(data['collecting'])
            with patch('app.read_device', return_value=real(obs_id='recovered')):
                self.poll()
            self.assertEqual(self.client.get('/rfid/data').json['connected'], '在线')
            self.assertEqual(self.query('SELECT * FROM alerts'), [])

    def test_inflight_response_discarded_after_switch(self):
        self.device()
        def fetch(_):
            conn = sqlite3.connect(self.path)
            conn.execute("UPDATE rfid_source_state SET mode='mock',generation=generation+1")
            conn.commit()
            conn.close()
            return real()
        with patch('app.read_device', side_effect=fetch):
            self.poll()
        self.assertEqual(self.query('SELECT * FROM rfid_batches'), [])

    def test_stale_clears_tags(self):
        self.device()
        with patch('app.read_device', return_value=real()):
            self.poll()
        with web.app.app_context():
            conn = get_db()
            conn.execute('UPDATE rfid_batches SET collected_at=?',
                         ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),))
            conn.commit()
        data = self.client.get('/rfid/data').json
        self.assertTrue(data['stale'])
        self.assertFalse(data['valid'])
        self.assertEqual(data['recent'], [])

    def test_permissions_csrf_and_bad_source(self):
        self.assertEqual(self.client.post('/rfid/source', data={'source': 'device'}).status_code, 400)
        self.assertEqual(self.post('/rfid/source', source='bad').status_code, 400)
        with self.client.session_transaction() as session:
            session['user_id'] = 2
        self.assertEqual(self.post('/rfid/source', source='device').status_code, 403)
        self.assertEqual(self.query('SELECT mode FROM rfid_source_state')[0]['mode'], 'mock')

    def test_inventory_check_modes(self):
        with patch('app.read_device', side_effect=AssertionError('no real read in mock')):
            self.post('/check/new')
        report = json.loads(self.query('SELECT result_report FROM inventory_check_tasks')[0]['result_report'])
        self.assertEqual(report['source'], 'mock')
        self.device()
        with patch('app.read_device', return_value=real()):
            self.post('/check/new')
        row = self.query('SELECT * FROM inventory_check_tasks ORDER BY id DESC')[0]
        self.assertEqual(row['status'], '不完整')
        self.assertEqual(json.loads(row['result_report'])['source'], 'device')
        with patch('app.read_device', side_effect=RfidError('no device')):
            self.post('/check/new')
        self.assertEqual(len(self.query('SELECT * FROM inventory_check_tasks')), 2)

    def test_templates_render_both_modes(self):
        for mode in ('mock', 'device'):
            self.post('/rfid/source', source=mode)
            for route in ('/', '/rfid', '/check', '/inventory'):
                response = self.client.get(route)
                self.assertEqual(response.status_code, 200, route)
                self.assertNotIn(b'test-token-at-least-16', response.data)
            self.post('/check/new') if mode == 'mock' else None
        self.assertEqual(self.client.get('/check/1').status_code, 200)

    def test_validation(self):
        data = {'stale': False, 'observation': real(['0011 2233', '00112233'])}
        self.assertEqual(validate_device(data, 10)['epc_list'], ['00112233'])
        for changes in ({'source': 'mock'}, {'valid': False}, {'epc_list': ['NOT-HEX']},
                        {'collected_at': '1970-01-01T00:00:00Z'}, {'collected_at': 'bad'}):
            with self.assertRaises(RfidError):
                validate_device({'stale': False, 'observation': real(**changes)}, 10)

    def test_http_contract(self):
        """验证 Python 确实发送约定的路径和密钥，而不只是 mock 函数返回值。"""
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, self.headers.get('X-RFID-Token')))
                data = json.dumps({'stale': False, 'observation': real()}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def log_message(self, *args):
                pass
        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            cfg = dict(web.app.config, RFID_API_URL='http://127.0.0.1:' + str(server.server_port))
            self.assertEqual(read_device(cfg)['source'], 'device')
            self.assertEqual(seen, [('/observations/latest', 'test-token-at-least-16')])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
