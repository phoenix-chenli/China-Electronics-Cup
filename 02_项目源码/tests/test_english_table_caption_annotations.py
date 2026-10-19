"""English printed table labels use the actual bound table's anchored caption."""
import copy

import pytest

from backend.table_bindings import check_table_bindings
from test_table_bindings import fixture


def english_fixture():
    record, table, text, binding = fixture()
    record.update(kind='text',page=277)
    table.update(format='pdf',page=277,
                 source_position={'page':277,'bbox':[.1,.2,.9,.9],'page_unit':'physical_page'},
                 caption={'text':'Table 3. Original statistics',
                          'evidence':{'page':277,'start':65,'end':93},'side':'before'})
    return record,table,text,binding


@pytest.mark.parametrize('label',['Table 3','TABLE 3','table 3','Table   3','Table\n3'])
def test_exact_english_caption_is_local_and_retains_source_anchor(label):
    record,table,text,binding=english_fixture();original=copy.deepcopy(record)
    full=label+'中，'+text;binding['statement']=full
    checked=check_table_bindings(full,[binding],[record])
    assert checked['valid'],checked['errors']
    annotations=[a for a in checked['context_annotations'] if a['kind']=='table_caption']
    assert len(annotations)==1 and annotations[0]['text']==label
    assert annotations[0]['evidence_id']=='E1' and annotations[0]['table_name']==table['table_name']
    assert annotations[0]['source_anchor']==table['caption']['evidence']
    assert full[annotations[0]['start']:annotations[0]['end']]==label
    assert record==original


@pytest.mark.parametrize('label',['Table 4','Table 30','Table 3.5','Table 3A','Table 3-2',
                                 'DataTable 3','Table 3元','Table 3 MGD'])
def test_wrong_partial_or_business_valued_label_not_annotated(label):
    record,_,text,binding=english_fixture();full=label+'中，'+text;binding['statement']=full
    checked=check_table_bindings(full,[binding],[record])
    assert not checked['valid']
    assert not any(a['kind']=='table_caption' for a in checked['context_annotations'])


@pytest.mark.parametrize('mutation',['no_caption','no_anchor','wrong_page','other_caption','not_a_caption','wrong_binding'])
def test_caption_must_belong_to_the_actual_same_page_bound_table(mutation):
    record,table,text,binding=english_fixture();full='Table 3中，'+text;binding['statement']=full
    if mutation=='no_caption':table.pop('caption')
    elif mutation=='no_anchor':table['caption'].pop('evidence')
    elif mutation=='wrong_page':table['caption']['evidence']['page']=276
    elif mutation=='other_caption':table['caption']['text']='Table 4. Other statistics'
    elif mutation=='not_a_caption':table['caption']['text']='See Table 3 for details'
    elif mutation=='wrong_binding':binding['table_name']='Other table'
    checked=check_table_bindings(full,[binding],[record])
    assert not checked['valid']
    assert not any(a['kind']=='table_caption' for a in checked['context_annotations'])


def test_same_numeric_amount_elsewhere_is_not_whitelisted():
    record,_,text,binding=english_fixture()
    sentence='Table 3中，'+text;full=sentence+'另一项收入为3万元。';binding['statement']=sentence
    checked=check_table_bindings(full,[binding],[record])
    assert not checked['valid'] and '3万元' in checked['numeric_text']
    assert len([a for a in checked['context_annotations'] if a['kind']=='table_caption'])==1


def test_caption_sentence_dot_is_not_a_decimal_identifier():
    record,_,text,binding=english_fixture();full='Table 3. 原始统计：'+text;binding['statement']=full
    checked=check_table_bindings(full,[binding],[record])
    assert checked['valid'],checked['errors']
    assert [a['text'] for a in checked['context_annotations'] if a['kind']=='table_caption']==['Table 3']


def test_another_cited_tables_caption_does_not_authorize_this_binding():
    record,table,text,binding=english_fixture();table['caption']['text']='Table 4. Actual table'
    other=copy.deepcopy(record);other['id']='E2'
    other['table_context']['tables'][0]['table_name']='Other table'
    other['table_context']['tables'][0]['caption']['text']='Table 3. Other table'
    full='Table 3中，'+text;binding['statement']=full
    checked=check_table_bindings(full,[binding],[record,other])
    assert not checked['valid']
    assert not any(a['kind']=='table_caption' for a in checked['context_annotations'])
