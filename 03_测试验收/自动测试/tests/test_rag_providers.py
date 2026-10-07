"""Local HTTP fixtures test provider contracts, not semantic retrieval accuracy."""
import json, math, os, tempfile, threading, time, unittest, zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from fastapi import HTTPException
from tests.test_conversations import LocalClient
from backend import connections as c, rag_providers as p


class Provider(BaseHTTPRequestHandler):
    calls = []
    mode = 'ok'

    def log_message(self, *args):
        pass

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.calls.append((self.path, data))
        if self.mode == 'slow':
            time.sleep(.7)
        if self.path.endswith('/embeddings'):
            rows = [{'index': i, 'embedding': [float(i + 1), 1., 2., 3., 4., 5., 6., 7.]} for i in range(len(data['input']))]
            if self.mode == 'duplicate':
                rows[-1]['index'] = 0
            if self.mode == 'zero':
                rows[0]['embedding'] = [0.] * 8
            if self.mode == 'nan':
                rows[0]['embedding'][0] = float('nan')
            if self.mode == 'dimension':
                rows[0]['embedding'].append(1.)
            if self.mode == 'constant':
                for row in rows:
                    row['embedding'] = [1.] * 8
            result = {'data': list(reversed(rows)), 'model': 'protocol-fixture', 'usage': {'prompt_tokens': 3, 'total_tokens': True, 'secret': 'omit'}}
        else:
            rows = [{'index': i, 'relevance_score': 10. - i} for i in range(len(data['documents']))]
            if self.mode == 'duplicate':
                rows[-1]['index'] = 0
            if self.mode == 'nan':
                rows[0]['relevance_score'] = float('nan')
            if self.mode == 'wrong':
                rows.reverse()
                for i, row in enumerate(rows):
                    row['relevance_score'] = 10. - i
            result = {'results': list(reversed(rows)), 'model': 'protocol-fixture'}
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())


class RagProviderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = LocalClient()
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.endpoint = 'http://127.0.0.1:' + str(cls.server.server_port) + '/v1'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.client.close()

    def setUp(self):
        Provider.mode = 'ok'
        Provider.calls.clear()
        old = p.config()
        p.save_config(p.ConfigRequest(expected_revision=old['revision']))

    def ready(self, role):
        result = self.client.post('/api/connections', json={'name': 'RAG 协议测试', 'kind': role, 'endpoint': self.endpoint, 'model': 'protocol-fixture'})
        self.assertEqual(result.status_code, 200, result.text)
        cid = result.json()['id']
        result = self.client.post('/api/connections/' + cid + '/test')
        self.assertTrue(result.json()['last_test']['ok'], result.text)
        result = self.client.patch('/api/connections/' + cid + '/enabled', json={'enabled': True})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def choose(self, role, connection, **extra):
        old = p.config()
        return p.save_config(p.ConfigRequest(expected_revision=old['revision'], **{role + '_mode': 'connection', role + '_connection_id': connection['id']}, **extra))

    def test_embedding_actual_probe_dimension_and_input_order(self):
        d = self.ready('embedding')
        self.assertEqual(d['last_test']['dimension'], 8)
        self.choose('embedding', d)
        result = p.embed(['第一段', '第二段'])
        self.assertEqual(len(result['vectors']), 2)
        self.assertLess(result['vectors'][0][0], result['vectors'][1][0])
        self.assertAlmostEqual(sum(v * v for v in result['vectors'][0]), 1.)
        self.assertEqual(result['usage'], {'prompt_tokens': 3})
        self.assertEqual(Provider.calls[-1][1]['input'], ['第一段', '第二段'])

    def test_embedding_rejects_invalid_vectors_and_duplicate_indices(self):
        d = self.ready('embedding')
        self.choose('embedding', d)
        for mode in ['duplicate', 'zero', 'nan', 'dimension']:
            with self.subTest(mode=mode):
                Provider.mode = mode
                with self.assertRaises(ValueError):
                    p.embed(['第一段', '第二段'])

    def test_constant_probe_never_grants_readiness(self):
        result = self.client.post('/api/connections', json={'name': '相同向量', 'kind': 'embedding', 'endpoint': self.endpoint, 'model': 'protocol-fixture'}).json()
        Provider.mode = 'constant'
        result = self.client.post('/api/connections/' + result['id'] + '/test').json()
        self.assertFalse(result['last_test']['ok'])
        self.assertFalse(result['enabled'])

    def test_rerank_preserves_candidate_indices_and_raw_scores(self):
        d = self.ready('rerank')
        self.choose('rerank', d)
        result = p.rerank('问句', ['原文甲', '原文乙'])
        self.assertEqual(result['scores'], [10., 9.])
        self.assertEqual(Provider.calls[-1][1]['documents'], ['原文甲', '原文乙'])
        for mode in ['duplicate', 'nan']:
            Provider.mode = mode
            with self.assertRaises(ValueError):
                p.rerank('问句', ['原文甲', '原文乙'])

    def test_wrong_rerank_probe_stays_disabled(self):
        d = self.ready('rerank')
        Provider.mode = 'wrong'
        result = self.client.post('/api/connections/' + d['id'] + '/test').json()
        self.assertFalse(result['last_test']['ok'])
        self.assertFalse(result['enabled'])

    def test_revision_conflict_and_provider_identity_before_sending(self):
        d = self.ready('embedding')
        self.choose('embedding', d)
        previous = p.describe('embedding')
        old = p.config()
        p.save_config(p.ConfigRequest(expected_revision=old['revision'], embedding_mode='disabled'))
        with self.assertRaises(HTTPException) as error:
            p.save_config(p.ConfigRequest(expected_revision=old['revision']))
        self.assertEqual(error.exception.status_code, 409)
        self.choose('embedding', d)
        before = len(Provider.calls)
        with self.assertRaisesRegex(ValueError, '身份已改变'):
            p.embed(['正文'], expected_fingerprint='not-' + previous['fingerprint'])
        self.assertEqual(len(Provider.calls), before)

    def test_remote_bulk_index_requires_both_permissions_before_transport(self):
        d = self.ready('embedding')
        self.choose('embedding', d)
        descriptor = {**p.describe('embedding'), 'local': False}
        before = len(Provider.calls)
        with patch.object(p, 'describe', return_value=descriptor):
            for opted in [False, True]:
                with self.assertRaisesRegex(ValueError, '发送预览'):
                    p.embed(['正文不得静默外传'], purpose='index', remote_index_opt_in=opted)
        self.assertEqual(len(Provider.calls), before)
        self.choose('embedding', d, remote_index_allowed=True)
        descriptor = {**p.describe('embedding'), 'local': False}
        with patch.object(p, 'describe', return_value=descriptor):
            with self.assertRaisesRegex(ValueError, '发送预览'):
                p.embed(['正文'], purpose='index')
            result = p.embed(['正文'], purpose='index', remote_index_opt_in=True)
        self.assertGreater(result['sent_bytes'], 0)

    def test_cancel_before_call_and_during_transport_is_prompt(self):
        d = self.ready('embedding')
        self.choose('embedding', d)
        event = threading.Event()
        event.set()
        before = len(Provider.calls)
        with self.assertRaises(InterruptedError):
            p.embed(['正文'], cancelled=event)
        self.assertEqual(len(Provider.calls), before)
        event.clear()
        Provider.mode = 'slow'
        threading.Timer(.1, event.set).start()
        start = time.monotonic()
        with self.assertRaises(InterruptedError):
            p.embed(['正文'], cancelled=event)
        self.assertLess(time.monotonic() - start, .5)
        time.sleep(.8)  # Finish this fixture's in-flight HTTP before changing shared mode.

    def test_invalid_batches_fail_before_http(self):
        d = self.ready('embedding')
        self.choose('embedding', d)
        before = len(Provider.calls)
        for texts in [[], [''], ['正文'] * 33, ['x' * 12001]]:
            with self.assertRaises(ValueError):
                p.embed(texts)
        self.assertEqual(len(Provider.calls), before)

    def test_missing_models_never_download_on_describe(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'EVIDENCEFLOW_RAG_MODEL_DIR': temp}), patch.object(p.httpx.Client, 'stream', side_effect=AssertionError('no automatic download')):
            result = p.capabilities()
            self.assertFalse(result['embedding']['ready'])
            self.assertFalse(result['rerank']['ready'])
            self.assertIn('未安装', result['embedding']['reason'])

    def test_tampered_bundled_model_fails_without_installing(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            pack = directory / 'bad.zip'
            with zipfile.ZipFile(pack, 'w') as archive:
                archive.writestr('bge-small-zh-v1.5/model.onnx', b'bad')
            with patch.dict(os.environ, {'EVIDENCEFLOW_RAG_MODEL_DIR': str(directory / 'models'), 'EVIDENCEFLOW_RAG_MODEL_PACK': str(pack)}):
                p._install('bge-small-zh-v1.5', False)
                self.assertEqual(p._jobs['bge-small-zh-v1.5']['status'], 'failed')
                self.assertFalse((directory / 'models/bge-small-zh-v1.5').exists())
                self.assertFalse(list((directory / 'models').glob('.install-*')))


if __name__ == '__main__':
    unittest.main()
