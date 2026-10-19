"""Executed tool labels do not authorize unrelated business numbers."""
import pytest
from backend.cross_source_engine import _without_citation_labels, _claims, CrossSourceError


@pytest.mark.parametrize('label',['计算K29。','已执行计算 K29，','计算结果：K29）','计算记录K29'])
def test_known_same_claim_calculation_label_is_local(label):
    text=label+' 实际金额29元。'
    masked=_without_citation_labels(text,[],['K29'])
    assert len(masked)==len(text) and 'K29' not in masked
    assert masked.endswith('实际金额29元。')


@pytest.mark.parametrize('text',['计算K29元。','计算K290。','计算K30。','产品K29。','K29','计算K29kg。'])
def test_unknown_nonlabel_or_unit_suffix_remains(text):
    assert _without_citation_labels(text,[],['K29'])==text


def test_reference_must_be_explicit_and_executed_for_this_claim():
    evidence={'E1':{'id':'E1','kind':'text','source_id':'s','quote':'金额为8元。'}}
    calc={'id':'K29','value':'8','display_value':'8.00','unit':'元','evidence_ids':['E1'],'parameters':[]}
    candidate={'text':'依据已执行计算K29，结果为8.00元。','evidence_ids':['E1'],'calculation_ids':['K29']}
    assert _claims({'claims':[candidate]},evidence,{'K29':calc})[0]['text']==candidate['text']
    with pytest.raises(CrossSourceError):
        _claims({'claims':[{**candidate,'calculation_ids':[]}]},evidence,{'K29':calc})
    with pytest.raises(CrossSourceError):
        _claims({'claims':[{**candidate,'text':candidate['text']+'另有29元。'}]},evidence,{'K29':calc})
