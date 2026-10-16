"""Provider protocol and evidence protections; fixture scores are not OCR quality."""
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch
import zipfile

import numpy as np
from PIL import Image
from backend import document_providers as p


def image():
    output = io.BytesIO(); Image.new('RGB', (100, 60), 'white').save(output, format='PNG')
    return output.getvalue()


def body():
    return {'blocks': [], 'tables': [], 'charts': [], 'issues': [], 'provenance': {}}


class DocumentProviderTests(unittest.TestCase):
    def test_fixed_paths_do_not_implicitly_install_or_download(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(p, 'model_dir', return_value=Path(folder)), patch.object(p.httpx, 'Client') as network:
            self.assertFalse(p.describe()['ready'])
            self.assertFalse(p.models_status()['items'][0]['installed'])
            network.assert_not_called()

    def test_install_requires_explicit_download_acceptance(self):
        with patch.object(p, 'bundled_pack', return_value=Path('missing-public-model-pack.zip')), patch.object(p.threading, 'Thread') as thread:
            with self.assertRaises(ValueError): p.install(False)
            with self.assertRaises(ValueError): p.install(1)
            thread.assert_not_called()

    def test_model_digest_changes_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); target = root/'model.onnx'; target.write_bytes(b'public model')
            files = {'model.onnx': (hashlib.sha256(b'public model').hexdigest(), 12, 'https://example.invalid/model')}
            with patch.object(p, 'FILES', files), patch.object(p, '_verified', None):
                p._verify(root)
                target.write_bytes(b'tamper model')
                with self.assertRaises(ValueError): p._verify(root)

    def test_bad_offline_pack_does_not_replace_installed_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); destination = root/p.MODEL_ID; destination.mkdir()
            (destination/'model.onnx').write_bytes(b'old')
            archive = root/'public.zip'
            with zipfile.ZipFile(archive, 'w') as pack:
                pack.writestr(p.MODEL_ID+'/model.onnx', b'bad')
            files = {'model.onnx': (hashlib.sha256(b'new').hexdigest(), 3, 'https://example.invalid/model')}
            with patch.object(p, 'FILES', files), patch.object(p, 'model_dir', return_value=destination), patch.object(p, 'bundled_pack', return_value=archive), patch.object(p, '_job', {}):
                p._install(False)
                self.assertEqual(p._job['status'], 'failed')
                self.assertEqual((destination/'model.onnx').read_bytes(), b'old')
                self.assertFalse(list(root.glob('.ocr-install-*')))

    def test_image_limits_and_rotation_types(self):
        with self.assertRaises(ValueError): p.ocr(image(), rotation=True)
        with self.assertRaises(ValueError): p.ocr(image(), rotation=45)
        with self.assertRaises(ValueError): p._image(b'not an image')
        output = io.BytesIO(); Image.new('RGB', (2401, 1)).save(output, format='PNG')
        with self.assertRaises(ValueError): p._image(output.getvalue())

    def test_four_clockwise_rotation_inverse_coordinates(self):
        expected = (.2, .3); width, height = 100, 60
        points = {0: (20, 18), 90: (42, 20), 180: (80, 42), 270: (18, 80)}
        for rotation, point in points.items():
            actual = p._original_point(*point, width, height, rotation)
            self.assertAlmostEqual(actual[0], expected[0]); self.assertAlmostEqual(actual[1], expected[1])

    def fake_ocr(self, engine, **options):
        provider = {'ready': True, 'fingerprint': 'fixed-public-fixture', 'model': p.MODEL}
        with patch.object(p, 'describe', return_value=provider), patch.object(p, '_load', return_value=(engine, [])):
            return p.ocr(image(), **options)

    def test_ocr_retains_real_line_geometry_and_low_score_pending(self):
        def engine(_pixels, **_):
            return types.SimpleNamespace(boxes=np.asarray([[[10, 10], [40, 10], [40, 20], [10, 20]]]), txts=('金额 12.50',), scores=(.7,))
        result = self.fake_ocr(engine)
        self.assertEqual(result['lines'][0]['text'], '金额 12.50')
        self.assertEqual(result['lines'][0]['status'], 'pending')
        self.assertEqual(len(result['lines'][0]['polygon']), 4)
        self.assertEqual(result['lines'][0]['location_precision'], 'line')
        self.assertNotIn('chars', result['lines'][0])
        self.assertEqual(result['input_image_sha256'], hashlib.sha256(image()).hexdigest())

    def test_ocr_rejects_nonfinite_and_out_of_bounds_outputs(self):
        for corners, score in [([[10, 10], [120, 10], [120, 20], [10, 20]], .99),
                               ([[10, 10], [40, 10], [40, 20], [10, 20]], float('nan'))]:
            with self.assertRaises(ValueError):
                self.fake_ocr(lambda *_args, **_kwargs: types.SimpleNamespace(boxes=np.asarray([corners]), txts=('12',), scores=(score,)))

    def test_ocr_cancellation_discards_late_inference(self):
        cancelled = threading.Event(); completed = threading.Event(); release = threading.Event()
        def slow(*_args, **_kwargs):
            cancelled.set(); release.wait(2); completed.set()
            return types.SimpleNamespace(boxes=(), txts=(), scores=())
        try:
            with self.assertRaises(InterruptedError): self.fake_ocr(slow, cancelled=cancelled)
            self.assertFalse(completed.is_set(), '调用必须在迟到推理完成前取消')
        finally:
            release.set()
        self.assertTrue(completed.wait(1))

    def test_expired_deadline_is_not_replaced_by_default(self):
        with self.assertRaises(TimeoutError): p.ocr(image(), deadline=0)
        with self.assertRaises(TimeoutError): p.visual(image(), 'id', 1, [], deadline=0)

    def test_strict_json_rejects_unknown_duplicate_nonfinite_and_types(self):
        invalid = ['{"blocks":[],"blocks":[],"tables":[],"charts":[],"issues":[],"provenance":{}}',
                   '```json\n'+json.dumps(body())+'\n```',
                   json.dumps({**body(), 'unexpected': True}),
                   '{"blocks":NaN,"tables":[],"charts":[],"issues":[],"provenance":{}}']
        for value in invalid:
            with self.assertRaises(ValueError): p._strict_json(value)
        for value in [[0, 0, 1, float('inf')], [False, 0, 1, 1], [0, 0, 1.01, 1], [0, 0, 0, 1]]:
            with self.assertRaises(ValueError): p._box(value)

    def test_visual_text_must_match_same_region_independent_observations(self):
        value = body(); value['blocks'] = [{'text': '金额 12', 'box': [.1, .1, .4, .3], 'kind': 'text'}]
        seen = p._observations([{'text': '金额 12', 'box': [.12, .12, .3, .2], 'confidence': .99}])
        self.assertEqual(p._validate_visual(value, seen)['blocks'][0]['status'], 'confirmed')
        seen = p._observations([{'text': '金额 12', 'box': [.6, .6, .9, .8]}])
        result = p._validate_visual(value, seen)
        self.assertEqual(result['blocks'][0]['status'], 'pending'); self.assertEqual(result['confirmed_text'], '')
        value['blocks'][0]['text'] = '金额 12'
        seen = p._observations([{'text': '金额 120', 'box': [.12, .12, .3, .2]}])
        self.assertEqual(p._validate_visual(value, seen)['blocks'][0]['status'], 'pending')

    def table(self):
        return {'title': '', 'box': [0, 0, 1, 1], 'rows': 1, 'columns': 2, 'cells': [
            {'row': 0, 'column': 0, 'rowspan': 1, 'colspan': 1, 'text': '金额', 'box': [0, 0, .5, 1], 'role': 'row_header'},
            {'row': 0, 'column': 1, 'rowspan': 1, 'colspan': 1, 'text': '120', 'box': [.5, 0, 1, 1], 'role': 'data'}]}

    def test_table_span_grid_and_geometry_conflicts_are_rejected(self):
        for change in ('logical_overlap', 'geometry_overlap', 'missing', 'bool_row', 'overspan', 'unknown'):
            value = body(); value['tables'] = [self.table()]; cells = value['tables'][0]['cells']
            if change == 'logical_overlap': cells[1]['column'] = 0
            if change == 'geometry_overlap': cells[1]['box'] = [0, 0, 1, 1]
            if change == 'missing': cells.pop()
            if change == 'bool_row': cells[0]['row'] = False
            if change == 'overspan': cells[0]['rowspan'] = 2
            if change == 'unknown': cells[0]['confidence'] = .99
            with self.assertRaises(ValueError, msg=change): p._validate_visual(value, [])

    def test_unobserved_or_conflicting_table_number_remains_pending(self):
        value = body(); value['tables'] = [self.table()]
        seen = p._observations([{'text': '金额', 'box': [.1, .1, .4, .2]}, {'text': '121', 'box': [.6, .1, .9, .2]}])
        result = p._validate_visual(value, seen)
        self.assertEqual(result['tables'][0]['cells'][0]['status'], 'confirmed')
        self.assertEqual(result['tables'][0]['cells'][1]['status'], 'pending')
        self.assertNotIn('120', result['confirmed_text'])

    def test_unobserved_table_title_does_not_confirm_whole_table(self):
        value = body(); value['tables'] = [self.table()]
        value['tables'][0]['title'] = '未经观察的标题'
        seen = p._observations([{'text': '金额', 'box': [.1, .1, .4, .2]}, {'text': '120', 'box': [.6, .1, .9, .2]}])
        result = p._validate_visual(value, seen)
        self.assertEqual(result['tables'][0]['title_status'], 'pending')
        self.assertEqual(result['tables'][0]['status'], 'pending')
        self.assertNotIn('未经观察的标题', result['confirmed_text'])

    def chart(self):
        return {'title': '', 'kind': 'bar', 'box': [0, 0, 1, 1], 'x_label': '期间', 'y_label': '收入', 'unit': '元',
                'series': [{'name': '项目', 'axis': 'primary', 'color': 'blue', 'points': [
                    {'label': '2026', 'value': '120', 'printed_text': '120', 'box': [.4, .4, .7, .6], 'value_source': 'printed'}]}]}

    def test_chart_estimation_unobserved_values_and_wrong_types_are_not_confirmed(self):
        # Axis titles and unit need their actual separate source positions;
        # finding these words together anywhere in the chart is insufficient.
        seen = p._observations([{'text':'期间','box':[.45,.9,.55,.95]},
                                {'text':'收入(元)','box':[.03,.35,.18,.4]},
                                {'text':'项目','box':[.7,.1,.8,.15]},
                                {'text': '2026 120', 'box': [.5, .45, .6, .5]}])
        value = body(); value['charts'] = [self.chart()]
        self.assertEqual(p._validate_visual(value, seen)['charts'][0]['series'][0]['points'][0]['status'], 'confirmed')
        value['charts'][0]['series'][0]['points'][0]['value_source'] = 'estimated'
        result = p._validate_visual(value, seen)
        self.assertEqual(result['charts'][0]['series'][0]['points'][0]['status'], 'pending')
        self.assertEqual(result['confirmed_text'], '')
        value['charts'][0]['series'][0]['points'][0]['value'] = 120
        with self.assertRaises(ValueError): p._validate_visual(value, seen)

    def test_chart_category_value_association_cannot_use_whole_chart_occurrences(self):
        value = body(); value['charts'] = [self.chart()]
        point = value['charts'][0]['series'][0]['points'][0]
        point['label'] = 'B'
        seen = p._observations([{'text': '期间 收入 元 项目', 'box': [.05, .8, .8, .9]},
                                {'text': 'A 120', 'box': [.5, .45, .6, .5]},
                                {'text': 'B 200', 'box': [.8, .45, .95, .5]}])
        result = p._validate_visual(value, seen)
        output = result['charts'][0]['series'][0]['points'][0]
        self.assertEqual(output['status'], 'pending')
        self.assertEqual(output['association_status'], 'pending')
        self.assertEqual(result['confirmed_text'], '')
        # An excessively large model box and a line containing both categories
        # still cannot make B=120 a directly observed category/value pair.
        point['box'] = [0, 0, 1, 1]; point['printed_text'] = 'A 120 B 200'
        seen = p._observations([{'text': '期间 收入 元 项目', 'box': [.05, .8, .8, .9]},
                                {'text': 'A 120 B 200', 'box': [.1, .45, .9, .5]}])
        self.assertEqual(p._validate_visual(value, seen)['charts'][0]['series'][0]['points'][0]['status'], 'pending')

    def test_chart_series_unit_and_axis_context_require_independent_observations(self):
        for missing in ('项目', '元', '期间', '收入'):
            value = body(); value['charts'] = [self.chart()]
            seen = p._observations([{'text': ' '.join(v for v in ['期间', '收入', '元', '项目'] if v != missing), 'box': [.05, .8, .8, .9]},
                                    {'text': '2026 120', 'box': [.5, .45, .6, .5]}])
            result = p._validate_visual(value, seen)
            series = result['charts'][0]['series'][0]
            self.assertEqual(series['status'], 'pending', missing)
            self.assertEqual(series['points'][0]['status'], 'pending', missing)
            self.assertEqual(result['confirmed_text'], '', missing)

    def test_visual_rechecks_connection_and_discards_changed_or_cancelled_result(self):
        provider = {'id': 'fixture', 'kind': 'vision', 'revision': 1, 'timeout': 5, 'model': 'test', 'endpoint': 'http://127.0.0.1/v1'}
        response = {'text': json.dumps(body()), 'model': 'actual-test', 'usage': {'total_tokens': 10, 'completion_tokens': True}}
        with patch.object(p.c, 'ready', return_value=provider) as ready, patch.object(p.c, 'chat', return_value=response):
            result = p.visual(image(), 'fixture', 1, [])
            self.assertGreaterEqual(ready.call_count, 4)
            self.assertEqual(result['raw_response'], response['text'])
            self.assertEqual(result['provenance']['usage'], {'total_tokens': 10})
            self.assertEqual(result['provider']['model'], 'test')
            self.assertEqual(result['provider']['actual_model'], 'actual-test')
        with patch.object(p.c, 'ready', side_effect=[provider, provider, {**provider, 'revision': 2}]), patch.object(p.c, 'chat', return_value=response):
            with self.assertRaises(ValueError): p.visual(image(), 'fixture', 1, [])
        cancelled = threading.Event()
        def late(*_args, **_kwargs):
            cancelled.set(); return response
        with patch.object(p.c, 'ready', return_value=provider), patch.object(p.c, 'chat', side_effect=late):
            with self.assertRaises(InterruptedError): p.visual(image(), 'fixture', 1, [], cancelled=cancelled)

    def test_visual_sends_exact_frozen_png_and_jpeg_bytes(self):
        import base64
        from PIL import Image
        provider={'id':'fixture','kind':'vision','revision':1,'timeout':5,'model':'test','endpoint':'http://127.0.0.1/v1'}
        response={'text':json.dumps(body()),'model':'actual-test','usage':{}}
        for fmt,mime in [('PNG','image/png'),('JPEG','image/jpeg')]:
            encoded=io.BytesIO();Image.new('RGB',(120,80),'white').save(encoded,format=fmt);raw=encoded.getvalue()
            with patch.object(p.c,'ready',return_value=provider),patch.object(p.c,'chat',return_value=response) as chat:
                result=p.visual(raw,'fixture',1,[])
                url=chat.call_args.args[1][0]['content'][1]['image_url']['url']
                self.assertTrue(url.startswith('data:'+mime+';base64,'))
                self.assertEqual(base64.b64decode(url.split(',',1)[1]),raw)
                self.assertEqual(result['provenance']['sent_image_sha256'],hashlib.sha256(raw).hexdigest())
                self.assertEqual(result['provenance']['sent_image_bytes'],len(raw))


if __name__ == '__main__':
    unittest.main()
