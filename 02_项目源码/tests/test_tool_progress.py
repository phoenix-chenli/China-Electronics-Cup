from backend.tool_progress import sql_fingerprint, observed_date_feedback

SOURCE={'id':'s','sha256':'abc','parser_version':'p1'}
CAT={'tables':[{'name':'inspections','details':{'column_profiles':[{'name':'period','distinct_values':['2025-01','2025-02']}]}}]}
SQL='SELECT i.period FROM inspections i WHERE i.period=?'

def test_fingerprint_semantic_format_and_source_parameter_changes():
    a=sql_fingerprint(SOURCE,SQL,['2025年2月'])
    assert a==sql_fingerprint(SOURCE,'SELECT i.period\nFROM inspections AS i WHERE i.period = ?',['2025年2月'])
    assert a!=sql_fingerprint(SOURCE,SQL,['2025-02'])
    assert a!=sql_fingerprint({**SOURCE,'parser_version':'p2'},SQL,['2025年2月'])
    assert a!=sql_fingerprint({**SOURCE,'sha256':'changed'},SQL,['2025年2月'])

def test_date_only_observed_exact_column_never_auto_rewrites():
    params=['2025年2月']
    r=observed_date_feedback(SOURCE,SQL,params,CAT)
    assert r[0]['observed_equivalent_spellings']==['2025-02']
    assert r[0]['parameter_index']==0 and params==['2025年2月']
    assert r[0]['scope']=='bounded_catalog_sample_not_answer_evidence'
    assert observed_date_feedback(SOURCE,SQL,['2025年3月'],CAT)==[]
    assert observed_date_feedback(SOURCE,'SELECT * FROM other WHERE period=?',params,CAT)==[]
    assert observed_date_feedback(SOURCE,'SELECT * FROM inspections WHERE other=?',params,CAT)==[]
    assert observed_date_feedback(SOURCE,'SELECT * FROM inspections WHERE period LIKE ?',params,CAT)==[]

def test_positional_parameter_order_is_not_ast_breadth_first():
    sql='SELECT * FROM inspections WHERE station=? AND (period=? AND code=?)'
    r=observed_date_feedback(SOURCE,sql,['甲','2025年2月','X'],CAT)
    assert r[0]['parameter_index']==1
    assert observed_date_feedback(SOURCE,'SELECT ? FROM inspections WHERE period=?',['2025年2月','x'],CAT)==[]
    assert observed_date_feedback(SOURCE,'SELECT * FROM inspections WHERE period=? OR period=?',['2025年2月','2025年1月'],CAT)==[]
