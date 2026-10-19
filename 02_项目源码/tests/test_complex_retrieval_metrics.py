from evaluation.complex_retrieval import page_metrics


def test_same_page_chunks_and_other_source_page_do_not_inflate_recall():
    results = [
        {'source_sha256': 'a', 'page': 250},
        {'source_sha256': 'a', 'page': 250},
        {'source_sha256': 'b', 'page': 251},
        {'source_sha256': 'a', 'page': 251},
    ]
    expected = {('a', 250), ('a', 251)}
    first = page_metrics(results, expected, 3)
    assert first['hit'] and first['matched_pages'] == 1
    assert first['annotated_page_recall'] == 0.5
    assert not first['all_annotated_pages_found']
    assert page_metrics(results, expected, 4)['all_annotated_pages_found']


def test_no_candidates_keeps_all_expected_pages_in_denominator():
    result = page_metrics([], {('a', 250), ('b', 19)}, 10)
    assert result['expected_pages'] == 2
    assert result['annotated_page_recall'] == 0
    assert not result['hit']
