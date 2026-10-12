"""Bounded input normalization: recorded changes only, and no rewrite to an internal code."""
import os
import tempfile
import unittest
from unittest.mock import patch

TEMP = tempfile.TemporaryDirectory(prefix='evidenceflow-query-normalize-')
os.environ['EVIDENCEFLOW_DATA_DIR'] = TEMP.name
from backend import query_normalize as qn, store as s
from backend.seed import seed_business


class QueryNormalizeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        s.init()
        seed_business()

    @classmethod
    def tearDownClass(cls):
        TEMP.cleanup()

    def test_traditional_table_is_well_formed_and_leaves_unknown_characters(self):
        table = qn._table()
        self.assertTrue(table)
        for item in qn._TABLE_PAIRS:
            self.assertIsInstance(item, str)
            self.assertEqual(len(item), 2)
            self.assertNotEqual(item[0], item[1])
        self.assertEqual(''.join(table.get(c, c) for c in '統計總額'), '统计总额')
        self.assertEqual(''.join(table.get(c, c) for c in '說國區產業'), '说国区产业')
        # A character outside the bounded table is never guessed at.
        self.assertEqual(''.join(table.get(c, c) for c in '鑫龍'), '鑫龙')

    def test_shape_and_alias_changes_are_recorded_with_a_source(self):
        result = qn.normalize('  新津區ＧＤＰ　是多少  ', data={'aliases': {'新津区': '成都市新津区'}, 'terms': []})
        self.assertTrue(result['applied'])
        self.assertEqual(result['normalized'], '成都市新津区GDP 是多少')
        kinds = [c['kind'] for c in result['changes']]
        self.assertEqual(kinds, ['unicode', 'whitespace', 'traditional', 'alias'])
        for change in result['changes']:
            self.assertTrue(change['from'])
            self.assertTrue(change['to'])
            self.assertIn('source', change)
        self.assertTrue(result['boundary'])

    def test_longest_alias_wins_and_absent_text_stays_untouched(self):
        # 存储 is a substring of 智能存储: applying the shorter alias first would
        # produce 智能云盘的销售额, so the expected text proves the longest-first order.
        data = {'aliases': {'存储': '云盘', '智能存储': '智能存储平台'}, 'terms': []}
        self.assertEqual(qn.normalize('智能存储的销售额', data=data)['normalized'], '智能存储平台的销售额')
        self.assertEqual(qn.normalize('存储的销售额', data=data)['normalized'], '云盘的销售额')
        plain = qn.normalize('今天天气怎么样', data=data)
        self.assertFalse(plain['applied'])
        self.assertEqual(plain['normalized'], '今天天气怎么样')
        self.assertEqual(plain['changes'], [])

    def test_single_character_typo_needs_one_unique_vocabulary_candidate(self):
        data = {'aliases': {}, 'terms': ['第三产业增加值', '地区生产总值']}
        fixed = qn.normalize('新津区第三产业增长值', data=data)
        self.assertEqual(fixed['normalized'], '新津区第三产业增加值')
        self.assertEqual([c['kind'] for c in fixed['changes']], ['typo'])
        # Two same-length candidates one substitution away: left as the user wrote it.
        ambiguous = qn.normalize('第一产业换值是多少', data={'aliases': {}, 'terms': ['第一产业增加值', '第一产业减少值']})
        self.assertFalse(ambiguous['applied'])
        self.assertEqual(ambiguous['normalized'], '第一产业换值是多少')

    def test_a_shifted_substring_is_never_rewritten(self):
        """difflib-style similarity accepted 景山区的 ~ 石景山区 and corrupted the span."""
        data = {'aliases': {}, 'terms': ['石景山区', '新津区']}
        result = qn.normalize('石景山区的数据', data=data)
        self.assertFalse(result['applied'])
        self.assertEqual(result['normalized'], '石景山区的数据')

    def test_two_character_terms_are_not_corrected(self):
        result = qn.normalize('上海的业务', data={'aliases': {}, 'terms': ['下海', '上海']})
        self.assertFalse(result['applied'])

    def test_changes_and_typo_corrections_are_bounded(self):
        aliases = {f'别称{i}': f'正式名称{i}' for i in range(30)}
        result = qn.normalize(' '.join(aliases), data={'aliases': aliases, 'terms': []})
        self.assertEqual(len(result['changes']), qn.MAX_CHANGES)
        terms = ['地区生产总值', '第三产业增加值', '主要工业产品产量', '一般公共预算收入', '城乡居民基本养老保险']
        question = '地区生产总查 第三产业增长值 主要工业产品产里 一般公共预算牧入 城乡居民基本养老保俭'
        typos = [c for c in qn.normalize(question, data={'aliases': {}, 'terms': terms})['changes'] if c['kind'] == 'typo']
        self.assertEqual(len(typos), qn.MAX_TYPOS)

    def test_lexicon_maps_metric_aliases_to_a_name_never_to_an_internal_code(self):
        s.save_definition({'kind': 'metric', 'name': '净销售总额', 'aliases': ['净销额'], 'target': 'net_sales'})
        s.save_definition({'kind': 'alias', 'name': '华东别称', 'aliases': ['东区'], 'field': 'region', 'target': '华东'})
        lexicon = qn.lexicon()
        self.assertEqual(lexicon['aliases']['净销额'], '净销售总额')
        self.assertEqual(lexicon['aliases']['东区'], '华东')
        self.assertNotIn('net_sales', lexicon['aliases'].values())
        result = qn.normalize('净销额是多少', data=lexicon)
        self.assertEqual(result['normalized'], '净销售总额是多少')


if __name__ == '__main__':
    unittest.main()
