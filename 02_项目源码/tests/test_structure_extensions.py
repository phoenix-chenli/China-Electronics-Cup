import copy
import pytest
from backend.document_structure import table_continuations
from backend.text_normalization import candidates,query_variants


def table(page,name='t',title='表1 费用',headers=('对象','费用（元）')):
    return {'page':page,'table_name':name,'caption':{'text':title,'evidence':{'page':page}},'column_count':2,
            'header_basis':'explicit','columns':[{'header_path':[{'text':h}]} for h in headers]}


def test_continuation_links_keep_original_tables_units_and_rows_separate():
    records=[table(1,'t1'),table(2,'t2',title='表1 费用（续）')];before=copy.deepcopy(records)
    result=table_continuations(records)
    assert len(result['links'])==1
    assert result['links'][0]['requires_semantic_review']
    assert not result['links'][0]['merged'] and not result['links'][0]['units_inherited']
    assert records==before


@pytest.mark.parametrize('change',[{'page':3},{'header_basis':'unknown'},{'structure_unknown':True},{'truncated':True},{'column_count':3},
    {'columns':[{'header_path':[{'text':'对象'}]},{'header_path':[{'text':'费用（万元）'}]}]}, {'caption':{'text':'表2 费用'}}])
def test_ambiguous_continuation_never_joins(change):
    second=table(2,'t2');second.update(change)
    assert not table_continuations([table(1,'t1'),second])['links']


def test_duplicate_matching_tables_on_previous_page_are_ambiguous():
    assert not table_continuations([table(1,'a'),table(1,'b'),table(2,'c')])['links']


def test_normalization_never_replaces_original_digits_units_or_quotation():
    text='國內資料 １２.５０億元 O0 l1';pages=[{'number':1,'text':text,'blocks':[]}];before=copy.deepcopy(pages)
    result=candidates(pages)
    assert result['items'][0]['candidate']=='国内资料'
    assert query_variants(text)[1]=='国内资料 １２.５０亿元 O0 l1'
    assert pages==before and not result['source_modified']
    for item in result['items']:
        p=item['source_position'];assert text[p['start']:p['end']]==item['original']
        assert not item['verified'] and not item['applied_to_source']


def test_spelling_candidates_need_low_confidence_and_known_original_vocabulary():
    low={'number':1,'text':'资科','blocks':[{'start':0,'end':2,'confidence':.4}]}
    assert candidates([low],['资料'])['items'][0]['candidate']=='资料'
    assert candidates([low])['items']==[]
    high=copy.deepcopy(low);high['blocks'][0]['confidence']=.99
    assert candidates([high],['资料'])['items']==[]
    assert candidates([{'number':1,'text':'1O.50','blocks':[{'start':0,'end':5,'confidence':.1}]}],['10.50'])['items']==[]
