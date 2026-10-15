"""Exact cell bindings and adversarial layouts, independent of OCR accuracy."""
import copy
import unittest
from backend import table_structure as t, document_native as n, document_providers as p, rag_index


def cell(row,column,text,role='data',rs=1,cs=1,status='confirmed'):
    return {'id':f'r{row}c{column}','row':row,'column':column,'rowspan':rs,'colspan':cs,
            'text':text,'role':role,'status':status,'box':[column/6,row/6,(column+cs)/6,(row+rs)/6]}


def complex_table():
    cells=[cell(0,0,'Region','corner',2),cell(0,1,'Item','corner',2),
           cell(0,2,'2024','column_header',cs=2),cell(0,4,'2025','column_header',cs=2)]
    for col,label in [(2,'Revenue (kCNY)'),(3,'Cost (kCNY)'),(4,'Revenue (kCNY)'),(5,'Cost (kCNY)')]:cells.append(cell(1,col,label,'column_header'))
    cells += [cell(2,0,'North','row_header',2),cell(4,0,'South','row_header',2)]
    for row,item,values in [(2,'A',['120','80','150','90']),(3,'B',['210','140','0','']),
                             (4,'C',['110','70','130','75']),(5,'D',['140','100','155','105'])]:
        cells.append(cell(row,1,item,'row_header'))
        cells.extend(cell(row,col,value) for col,value in enumerate(values,2))
    return {'id':'complex','rows':6,'columns':6,'title':'Sales','cells':cells,'box':[0,0,1,1],
            'method':'visual_observed','status':'confirmed'}


class TableStructureTests(unittest.TestCase):
    def test_multi_level_headers_merged_rows_and_units_bind_to_actual_source_ids(self):
        table=complex_table();views=list(t.row_views(table))
        self.assertEqual(len(views),4)
        second=next(view for view in views if view['row']==3)
        self.assertIn('North / B',second['text']);self.assertIn('2025 / Revenue (kCNY)：0',second['text'])
        self.assertNotIn('Cost (kCNY)：0',second['text'])
        binding=next(v for v in second['cell_bindings'] if v['value_cell_id']=='r3c4')
        self.assertEqual(binding['column_header_ids'],['r0c4','r1c4'])
        self.assertEqual(binding['row_header_ids'],['r2c0','r3c1'])
        self.assertTrue(set(binding['column_header_ids']+binding['row_header_ids'])<=set(second['cell_ids']))
        self.assertEqual(sum(c['text']=='North' for c in table['cells']),1)
    def test_one_uncertain_leaf_excludes_only_its_column(self):
        table=complex_table();next(c for c in table['cells'] if c['id']=='r1c5')['status']='pending';table['status']='pending'
        views=list(t.row_views(table));self.assertEqual(len(views),4)
        self.assertTrue(all('2025 / Cost' not in v['text'] for v in views))
        self.assertIn('2024 / Cost (kCNY)：80',views[0]['text'])
    def test_uncertain_row_name_does_not_leak_into_anonymous_value_facts(self):
        table=complex_table();next(c for c in table['cells'] if c['text']=='North')['status']='pending';table['status']='pending'
        self.assertEqual([v['row'] for v in t.row_views(table)],[4,5])
    def test_merged_data_does_not_take_only_the_first_leaf_header(self):
        table=complex_table();table['cells']=[c for c in table['cells'] if c['id'] not in {'r2c2','r2c3'}]
        table['cells'].append(cell(2,2,'shared note',cs=2))
        row=next(v for v in t.row_views(table) if v['row']==2)
        self.assertNotIn('shared note',row['text']);self.assertIn('2025 / Revenue (kCNY)：150',row['text'])
    def test_logical_cell_swap_with_nonoverlapping_real_rectangles_is_not_confirmed(self):
        table=complex_table();a=next(c for c in table['cells'] if c['id']=='r2c2');b=next(c for c in table['cells'] if c['id']=='r2c3')
        a['column'],b['column']=b['column'],a['column']
        self.assertEqual(list(t.row_views(table)),[]);self.assertEqual(table['relation_status'],'pending')
    def test_blank_remains_blank_and_is_not_forward_filled_or_changed_to_zero(self):
        table=complex_table();views=list(t.row_views(table));row=next(v for v in views if v['row']==3)
        self.assertNotIn('2025 / Cost',row['text']);self.assertEqual(next(c for c in table['cells'] if c['id']=='r3c5')['text'],'')
        self.assertTrue(any(b['value_cell_id']=='r3c4' for b in row['cell_bindings']))
    def test_native_second_header_band_is_recovered_without_turning_numeric_rows_into_headers(self):
        table=complex_table();table['method']='pdf_native_table'
        for c in table['cells']:
            if c['row']>0:c['role']='data'
        views=list(t.row_views(table))
        self.assertIn('2024 / Revenue (kCNY)：120',views[0]['text'])
        self.assertEqual(next(c for c in table['cells'] if c['id']=='r2c2')['role'],'data')
    def test_table_cell_binding_reaches_rag_quality_without_dropping_parent_headers(self):
        table=complex_table();page=n._page(1);n._table_rows(page,table,'visual_observed')
        block=page['blocks'][0];quality=rag_index._extraction_quality(page,block['start'],block['end'])
        self.assertIn('r0c2',quality['cell_ids']);self.assertIn('table_cell_bindings',quality)
        self.assertEqual(quality['table_cell_bindings'][0]['row_path'],['North','A'])
    def test_external_native_headers_supply_labels_and_citable_identity(self):
        table={'id':'e','rows':1,'columns':2,'method':'pdf_native_table','status':'confirmed',
               'cells':[cell(0,0,'Alpha','row_header'),cell(0,1,'10')],
               'external_header':{'cells':[{'id':'eh','row':-1,'column':1,'text':'Revenue','status':'confirmed','role':'column_header'}]}}
        row=list(t.row_views(table))[0];self.assertIn('Revenue：10',row['text']);self.assertIn('eh',row['cell_ids'])
    def test_cross_page_multilevel_signature_is_order_independent_and_idempotent(self):
        a=complex_table();a['box']=[.1,.7,.9,.95];b=copy.deepcopy(a);b.update(id='other',box=[.1,.1,.9,.35]);b['cells'].reverse()
        pages=[{'number':1,'tables':[a]},{'number':2,'tables':[b]}]
        self.assertEqual(len(n.link_continuations(pages)),1);n.link_continuations(pages)
        self.assertEqual(len(b['continuations']),1)
        next(c for c in b['cells'] if c['id']=='r0c2')['rowspan']=2
        self.assertEqual(n.link_continuations(pages),[])
        self.assertEqual(b['continuations'],[])
    def test_year_valued_body_is_not_another_header_band(self):
        table=complex_table();table['method']='docx_native'
        for c in table['cells']:
            if c['row']>0:c['role']='data'
            if c['row']>=2 and c['column']>=2:c['text']='2024'
        views=list(t.row_views(table))
        self.assertEqual(len(views),4)
        self.assertTrue(all(b['row']>=2 for v in views for b in v['cell_bindings']))
        self.assertEqual(next(c for c in table['cells'] if c['id']=='r2c2')['role'],'data')
    def test_text_valued_body_keeps_data_instead_of_becoming_header_or_stub(self):
        for merged in (False,True):
            with self.subTest(merged=merged):
                first=1 if merged else 0
                cells=([cell(0,0,'Contact','header',cs=2)] if merged else [])
                cells += [cell(first,0,'Name','header'),cell(first,1,'Email','header'),
                          cell(first+1,0,'Alice'),cell(first+1,1,'alice@example.com')]
                table={'id':'text','rows':first+2,'columns':2,'cells':cells,'method':'docx_native','status':'confirmed'}
                views=list(t.row_views(table));self.assertEqual(len(views),1)
                self.assertIn('Name：Alice',views[0]['text']);self.assertIn('Email：alice@example.com',views[0]['text'])
    def test_shared_numeric_value_is_not_allocated_to_each_distinct_row_label(self):
        table={'id':'shared','rows':3,'columns':2,'status':'confirmed','method':'docx_native',
            'cells':[cell(0,0,'Item','header'),cell(0,1,'Revenue','header'),cell(1,0,'A','row_header'),
                     cell(2,0,'B','row_header'),cell(1,1,'300',rs=2)]}
        self.assertEqual(list(t.row_views(table)),[])
        self.assertEqual(next(c for c in table['cells'] if c['text']=='300')['text'],'300')
    def test_same_group_merged_value_is_exposed_only_once(self):
        table={'id':'shared','rows':3,'columns':2,'status':'confirmed','method':'docx_native',
            'cells':[cell(0,0,'Item','header'),cell(0,1,'Revenue','header'),cell(1,0,'A','row_header',rs=2),cell(1,1,'300',rs=2)]}
        self.assertEqual(sum(len(v['cell_bindings']) for v in t.row_views(table)),1)
    def test_unknown_cell_role_does_not_turn_into_a_data_fact(self):
        table=complex_table();next(c for c in table['cells'] if c['id']=='r2c2')['role']='unknown'
        views=list(t.row_views(table))
        self.assertFalse(any(b['value_cell_id']=='r2c2' for v in views for b in v['cell_bindings']))
        self.assertIn({'cell_id':'r2c2','reason':'unknown_cell_role'},table['relation_audit']['unbound_values'])
        self.assertEqual(table['status'],'pending')
    def test_blank_row_name_does_not_create_an_anonymous_numeric_fact(self):
        table=complex_table();next(c for c in table['cells'] if c['id']=='r3c1')['text']=''
        self.assertEqual([v['row'] for v in t.row_views(table)],[2,4,5])
    def test_unknown_leaf_row_name_does_not_turn_detail_into_a_group_total(self):
        table=complex_table();next(c for c in table['cells'] if c['id']=='r2c1')['role']='unknown'
        views=list(t.row_views(table));self.assertEqual([v['row'] for v in views],[3,4,5])
        self.assertFalse(any(b['row_path']==['North'] for v in views for b in v['cell_bindings']))
        self.assertEqual(table['status'],'pending')
    def test_unknown_entire_leaf_header_band_is_not_silently_dropped(self):
        for columns in (2,3):
            with self.subTest(columns=columns):
                cells=[cell(0,0,'2024','column_header',cs=2),cell(1,0,'Revenue','unknown'),
                       cell(1,1,'Cost','unknown'),cell(2,0,'120'),cell(2,1,'80')]
                if columns==3:cells += [cell(0,2,'Count','column_header',rs=2),cell(2,2,'5')]
                table={'id':'unknown-band','rows':3,'columns':columns,'cells':cells,'method':'visual_observed','status':'confirmed'}
                views=list(t.row_views(table))
                self.assertEqual([b['column'] for v in views for b in v['cell_bindings']],[2] if columns==3 else [])
                self.assertIn({'cell_id':'r2c0','reason':'unresolved_column_label'},table['relation_audit']['unbound_values'])
    def test_missing_row_label_or_leaf_header_does_not_borrow_another_one(self):
        table=complex_table();table['cells']=[c for c in table['cells'] if c['id']!='r3c1']
        self.assertEqual([v['row'] for v in t.row_views(table)],[2,4,5])
        self.assertEqual(table['relation_audit']['coverage_status'],'partial')
        table=complex_table();table['cells']=[c for c in table['cells'] if c['id']!='r1c5']
        self.assertTrue(all(not any(b['column']==5 for b in v['cell_bindings']) for v in t.row_views(table)))
    def test_multiple_visual_tables_have_globally_distinct_cell_identity(self):
        one=complex_table();two=complex_table();t.prepare(one);t.prepare(two)
        after=n.apply_visual_page(n._page(1),{'tables':[one,two]})
        all_ids=[c['id'] for z in after['tables'] for c in z['cells']]
        self.assertEqual(len(all_ids),len(set(all_ids)))
        self.assertTrue(all(set(b['cell_ids']) <= {c['id'] for z in after['tables'] if z['id']==b['table_id'] for c in z['cells']}
                            for b in after['blocks'] if b.get('cell_ids')))
    def test_raw_text_crossing_table_boundary_cannot_bypass_pending_leaf(self):
        page=n._page(1);n._append(page,'uncertain total 105 and nearby note',box=[.0,.0,1.,1.],status='confirmed')
        n._append(page,'independent paragraph',box=[.0,.92,1.,1.],status='confirmed')
        table=complex_table();table['box']=[.2,.2,.8,.8]
        next(c for c in table['cells'] if c['id']=='r1c5')['status']='pending'
        # Geometry for this test is already checked separately; table bounds
        # here deliberately overlap less than 80% of a larger text block.
        table['method']='docx_native';page['tables']=[table];n._table_rows(page,table,'docx_native')
        n._protect_table_regions(page)
        evidence=''.join(page['text'][a:b] for a,b in rag_index._confirmed_ranges(page))
        self.assertNotIn('uncertain total',evidence);self.assertIn('independent paragraph',evidence)
        self.assertIn('2024 / Revenue (kCNY)：120',evidence)
