"""Independent chart goldens and adversarial associations; no model calls."""
import copy
import unittest

from backend import chart_structure as c


def line(identity, text, box, **extra):
    return {'id': identity, 'text': text, 'box': box, **extra}


def fixture(horizontal=False):
    chart = {'id': 'page1-chart1', 'title': 'Quarterly revenue', 'kind': 'bar', 'box': [0, 0, 1, 1],
             'x_label': 'Region' if horizontal else 'Quarter', 'y_label': 'Region' if horizontal else 'Revenue',
             'unit': 'kCNY', 'series': [{'name': 'Sales', 'axis': 'primary', 'points': []}]}
    observed = [line('title', 'Quarterly revenue', [.25, .03, .75, .08]),
                line('legend', 'Sales', [.7, .11, .9, .15])]
    if horizontal:
        chart['x_label'] = 'Revenue'
        observed += [line('x-axis', 'Revenue(kCNY)', [.4, .9, .6, .94]),
                     line('y-axis', 'Region', [.02, .4, .10, .44]),
                     line('cat-a', 'A', [.18, .3, .25, .34]), line('val-a', '120', [.6, .3, .65, .34]),
                     line('cat-b', 'B', [.18, .6, .25, .64]), line('val-b', '80', [.8, .6, .85, .64])]
    else:
        observed += [line('x-axis', 'Quarter', [.3, .92, .7, .96]),
                     line('y-axis', 'Revenue(kCNY)', [.02, .35, .14, .39]),
                     line('cat-a', 'A', [.3, .82, .4, .86]), line('val-a', '120', [.33, .3, .37, .34]),
                     line('cat-b', 'B', [.6, .82, .7, .86]), line('val-b', '80', [.63, .5, .67, .54])]
    for label, value in [('A', '120'), ('B', '80')]:
        source = next(item for item in observed if item['id'] == 'val-'+label.lower())
        chart['series'][0]['points'].append({'id': 'p-'+label, 'label': label, 'value': value,
            'printed_text': value, 'box': source['box'][:], 'value_source': 'printed'})
    return chart, observed


def points(chart):
    return [point for series in chart['series'] for point in series['points']]


class ChartStructureTests(unittest.TestCase):
    def test_independent_vertical_bar_labels_and_values_have_source_bound_goldens(self):
        chart, observed = fixture(); c.prepare(chart, observed)
        self.assertEqual(chart['status'], 'confirmed')
        views = list(c.point_views(chart)); self.assertEqual(len(views), 2)
        first = views[0]['source_binding']
        self.assertEqual((first['label'], first['value'], first['unit_text']), ('A', '120', 'kCNY'))
        self.assertEqual((first['category']['observation_id'], first['printed_value']['observation_id']), ('cat-a', 'val-a'))
        self.assertEqual(first['unit']['observation_id'], 'y-axis')
        self.assertEqual((first['chart_id'], first['point_id']), ('page1-chart1', 'p-A'))
        self.assertEqual(first['orientation'], 'vertical')

    def test_horizontal_bar_has_explicit_numeric_axis_and_aligned_category(self):
        chart, observed = fixture(True); c.prepare(chart, observed)
        self.assertEqual(chart['status'], 'confirmed')
        self.assertEqual(points(chart)[0]['source_binding']['orientation'], 'horizontal')
        self.assertEqual(points(chart)[0]['source_binding']['unit']['axis'], 'x_axis')

    def test_swapping_model_values_cannot_bind_them_to_wrong_categories(self):
        chart, observed = fixture(); a, b = points(chart)
        a['value'], b['value'] = b['value'], a['value']; a['printed_text'], b['printed_text'] = a['value'], b['value']
        a['box'], b['box'] = b['box'], a['box']
        c.prepare(chart, observed)
        self.assertEqual(list(c.point_views(chart)), [])

    def test_full_chart_point_boxes_do_not_prove_an_arbitrary_pair(self):
        chart, observed = fixture(); a, b = points(chart)
        a['value'], b['value'] = b['value'], a['value']; a['printed_text'], b['printed_text'] = a['value'], b['value']
        for point in points(chart): point['box'] = [0, 0, 1, 1]
        c.prepare(chart, observed)
        self.assertEqual(list(c.point_views(chart)), [])

    def test_numeric_prefix_sign_decimal_and_percentage_are_not_truncated(self):
        for printed in ('120', '-12', '12.5', '12%', '12.5%', '12e3', '12,000'):
            with self.subTest(printed=printed):
                chart, observed = fixture(); point = points(chart)[0]
                point.update(value='12', printed_text=printed)
                next(item for item in observed if item['id'] == 'val-a')['text'] = printed
                c.prepare(chart, observed)
                self.assertEqual(point['status'], 'pending')

    def test_repeated_numbers_at_distinct_positions_keep_actual_category_anchors(self):
        chart, observed = fixture(); points(chart)[1].update(value='120', printed_text='120')
        next(item for item in observed if item['id'] == 'val-b')['text'] = '120'
        c.prepare(chart, observed)
        self.assertEqual(chart['status'], 'confirmed')
        self.assertEqual([v['source_binding']['printed_value']['observation_id'] for v in c.point_views(chart)], ['val-a', 'val-b'])

    def test_repeated_category_text_with_distinct_geometry_is_not_merged(self):
        chart, observed = fixture(); points(chart)[1]['label'] = 'A'
        next(item for item in observed if item['id'] == 'cat-b')['text'] = 'A'
        c.prepare(chart, observed)
        self.assertEqual(chart['status'], 'confirmed')
        self.assertEqual([v['source_binding']['category']['observation_id'] for v in c.point_views(chart)], ['cat-a', 'cat-b'])

    def test_two_identical_points_cannot_reuse_one_observed_pair(self):
        chart, observed = fixture(); duplicate = copy.deepcopy(points(chart)[0]); duplicate['id'] = 'duplicate'
        chart['series'][0]['points'].append(duplicate)
        c.prepare(chart, observed)
        self.assertTrue(all(v['point_id'] != 'p-A' for v in c.point_views(chart)))
        self.assertNotEqual(duplicate['status'], 'confirmed')

    def test_duplicate_observation_identity_with_conflicting_boxes_is_excluded(self):
        chart, observed = fixture(); other = copy.deepcopy(observed[-1]); other['box'] = [.2, .2, .3, .3]; observed.append(other)
        c.prepare(chart, observed)
        self.assertIn('val-b', chart['relation_audit']['conflicting_observation_ids'])
        self.assertEqual(points(chart)[1]['status'], 'pending')

    def test_low_confidence_category_does_not_become_an_unnamed_number(self):
        chart, observed = fixture(); next(item for item in observed if item['id'] == 'cat-a')['confidence'] = .65
        c.prepare(chart, observed)
        self.assertEqual(list(c.point_views(chart)), [])
        self.assertEqual(chart['relation_audit']['confirmed_points'], 0)

    def test_missing_unit_and_unit_only_in_title_do_not_prove_axis_units(self):
        for kind in ('missing', 'title', 'neighbor'):
            with self.subTest(kind=kind):
                chart, observed = fixture(); next(item for item in observed if item['id'] == 'y-axis')['text'] = 'Revenue'
                if kind == 'title':
                    chart['title'] += '(kCNY)'; observed[0]['text'] = chart['title']
                elif kind == 'neighbor':
                    observed.append(line('unrelated-unit', 'kCNY', [.8, .35, .9, .39]))
                c.prepare(chart, observed)
                self.assertEqual(list(c.point_views(chart)), [])

    def test_standalone_unit_beside_numeric_axis_has_a_real_anchor(self):
        chart, observed = fixture(); next(item for item in observed if item['id'] == 'y-axis')['text'] = 'Revenue'
        observed.append(line('axis-unit', 'kCNY', [.025, .28, .13, .32]))
        c.prepare(chart, observed)
        self.assertEqual(points(chart)[0]['source_binding']['unit']['observation_id'], 'axis-unit')

    def test_axis_model_text_already_containing_exact_unit_suffix_is_not_double_wrapped(self):
        for horizontal, axis, text in [(False, 'y_label', 'Revenue (kCNY)'),
                                        (True, 'x_label', 'Revenue [kCNY]'),
                                        (True, 'x_label', '销售额（万元）')]:
            with self.subTest(axis=axis, text=text):
                chart, observed = fixture(horizontal); chart[axis] = text
                if '万元' in text: chart['unit'] = '万元'
                source = 'x-axis' if horizontal else 'y-axis'
                next(item for item in observed if item['id'] == source)['text'] = text
                c.prepare(chart, observed)
                self.assertEqual(chart['status'], 'confirmed')
                self.assertEqual(points(chart)[0]['source_binding']['unit']['observation_id'], source)

    def test_existing_axis_suffix_must_be_the_same_declared_unit(self):
        for text in ('Revenue (USD)', 'Revenue (not kCNY)', 'kCNY', 'Revenue (kCNY) per item'):
            with self.subTest(text=text):
                chart, observed = fixture(); chart['y_label'] = text
                next(item for item in observed if item['id'] == 'y-axis')['text'] = text
                c.prepare(chart, observed)
                self.assertEqual(list(c.point_views(chart)), [])

    def test_small_tight_numeric_point_boxes_keep_whole_observed_number_and_source_box(self):
        for horizontal in (False, True):
            with self.subTest(horizontal=horizontal):
                chart, observed = fixture(horizontal)
                for point in points(chart):
                    box = point['box']; point['box'] = [box[0]+.002, box[1]+.007, box[2]-.002, box[3]-.007]
                c.prepare(chart, observed)
                self.assertEqual(chart['status'], 'confirmed')
                self.assertEqual(points(chart)[0]['source_binding']['printed_value']['box'],
                                 next(item for item in observed if item['id'] == 'val-a')['box'])

    def test_small_numeric_box_offset_has_bounded_tolerance(self):
        chart, observed = fixture()
        for point in points(chart):
            box = point['box']; point['box'] = [box[0]+.005, box[1]+.003, box[2]+.005, box[3]+.003]
        c.prepare(chart, observed)
        self.assertEqual(chart['status'], 'confirmed')

    def test_numeric_location_tolerance_rejects_sliver_far_offset_and_nonlocal_region(self):
        for region in ([.345, .315, .355, .325], [.348, .3, .388, .34],
                       [.375, .3, .415, .34], [.1, .28, .6, .315]):
            with self.subTest(region=region):
                chart, observed = fixture(); points(chart)[0]['box'] = region
                c.prepare(chart, observed)
                self.assertEqual(points(chart)[0]['status'], 'pending')

    def test_location_tolerance_does_not_make_a_numeric_prefix_supported(self):
        chart, observed = fixture(); point = points(chart)[0]
        point.update(value='12', printed_text='12', box=[.332, .307, .368, .333])
        c.prepare(chart, observed)
        self.assertEqual(point['status'], 'pending')

    def test_swapping_model_axis_names_is_rejected_by_observed_axis_positions(self):
        chart, observed = fixture(); chart['x_label'], chart['y_label'] = chart['y_label'], chart['x_label']
        c.prepare(chart, observed)
        self.assertEqual(list(c.point_views(chart)), [])

    def test_series_axis_unknown_secondary_or_dual_axis_stays_pending(self):
        for axis, flag in [('unknown', False), ('secondary', False), ('primary', True)]:
            with self.subTest(axis=axis, dual=flag):
                chart, observed = fixture(); chart['series'][0]['axis'] = axis; chart['dual_axis'] = flag
                c.prepare(chart, observed)
                self.assertEqual(list(c.point_views(chart)), [])

    def test_multiseries_legend_elsewhere_does_not_prove_point_colour(self):
        chart, observed = fixture(); second = copy.deepcopy(chart['series'][0]); second['name'] = 'Cost'; chart['series'].append(second)
        observed.append(line('second-legend', 'Cost', [.7, .17, .9, .21]))
        c.prepare(chart, observed)
        self.assertEqual(list(c.point_views(chart)), [])

    def test_multiseries_same_line_explicit_series_category_and_number_is_supported(self):
        chart, observed = fixture(); first = chart['series'][0]; first['points'] = first['points'][:1]
        first['points'][0].update(box=[.3, .3, .65, .34], printed_text='Sales A 120')
        second = {'name': 'Cost', 'axis': 'primary', 'points': [{'id': 'cost-a', 'label': 'A', 'value': '80',
                  'printed_text': 'Cost A 80', 'box': [.3, .5, .65, .54], 'value_source': 'printed'}]}
        chart['series'].append(second)
        observed = [item for item in observed if not item['id'].startswith(('cat-', 'val-'))]
        observed += [line('sales-pair', 'Sales A 120', [.3, .3, .65, .34]),
                     line('cost-pair', 'Cost A 80', [.3, .5, .65, .54])]
        c.prepare(chart, observed)
        self.assertEqual([view['source_binding']['series_name'] for view in c.point_views(chart)], ['Sales', 'Cost'])

    def test_pending_sibling_does_not_erase_an_independently_observed_inline_point(self):
        chart, observed = fixture(); first = points(chart)[0]
        first.update(box=[.3, .3, .5, .34], printed_text='A 120')
        observed = [item for item in observed if not item['id'].startswith(('cat-', 'val-'))]
        observed.append(line('pair', 'A 120', [.3, .3, .5, .34]))
        c.prepare(chart, observed)
        self.assertEqual(chart['status'], 'pending')
        self.assertEqual(chart['relation_status'], 'partial')
        self.assertEqual([view['point_id'] for view in c.point_views(chart)], ['p-A'])

    def test_separate_line_scatter_line_pie_and_estimated_values_never_become_exact(self):
        for kind in ('line', 'scatter', 'pie', 'stacked_bar'):
            with self.subTest(kind=kind):
                chart, observed = fixture(); chart['kind'] = kind; c.prepare(chart, observed)
                self.assertEqual(list(c.point_views(chart)), [])
        chart, observed = fixture()
        for point in points(chart): point['value_source'] = 'estimated'
        c.prepare(chart, observed); self.assertEqual(list(c.point_views(chart)), [])

    def test_one_separated_point_does_not_supply_an_axis_geometry_pattern(self):
        chart, observed = fixture(); chart['series'][0]['points'] = points(chart)[:1]
        c.prepare(chart, observed); self.assertEqual(list(c.point_views(chart)), [])

    def test_same_line_complete_pair_supported_but_numeric_category_concatenation_rejected(self):
        chart, observed = fixture(); chart['series'][0]['points'] = points(chart)[:1]
        point = points(chart)[0]; point.update(box=[.3, .3, .5, .34], printed_text='A 120')
        observed = [item for item in observed if not item['id'].startswith(('cat-', 'val-'))]
        observed.append(line('pair', 'A 120', [.3, .3, .5, .34])); c.prepare(chart, observed)
        self.assertEqual(len(list(c.point_views(chart))), 1)
        point['label'] = '2024'; point['printed_text'] = '120'; observed[-1]['text'] = '2024120'
        c.prepare(chart, observed); self.assertEqual(list(c.point_views(chart)), [])

    def test_source_from_outside_chart_cannot_supply_missing_units_or_labels(self):
        chart, observed = fixture(); chart['box'] = [0, 0, .75, 1]
        # Every required actual plot observation remains inside except legend.
        c.prepare(chart, observed)
        self.assertEqual(list(c.point_views(chart)), [])

    def test_recompute_removes_stale_success_and_views_reject_mutated_shared_metadata(self):
        chart, observed = fixture(); c.prepare(chart, observed)
        chart['unit'] = 'USD'; self.assertEqual(list(c.point_views(chart)), [])
        chart['unit'] = 'kCNY'; chart['title'] = 'Invented new chart'; self.assertEqual(list(c.point_views(chart)), [])
        chart['title'] = 'Quarterly revenue'; c.prepare(chart, observed[:-1])
        self.assertEqual(list(c.point_views(chart)), [])

    def test_final_chart_and_point_ids_are_rebound_without_changing_observations(self):
        chart, observed = fixture(); chart.pop('id')
        for point in points(chart): point.pop('id')
        c.prepare(chart, observed); self.assertEqual(list(c.point_views(chart)), [])
        chart['id'] = 'page9-media2-chart3'
        for index, point in enumerate(points(chart)): point['id'] = chart['id']+'-p'+str(index)
        views = list(c.point_views(chart)); self.assertEqual(len(views), 2)
        self.assertEqual(views[0]['source_binding']['chart_id'], chart['id'])
        self.assertEqual(views[0]['source_binding']['printed_value']['observation_id'], 'val-a')

    def test_visual_view_rejects_stale_axis_kind_and_region_claims(self):
        for field, value in [('axis', 'secondary'), ('kind', 'line'), ('box', [.01, .01, .99, .99])]:
            with self.subTest(field=field):
                chart, observed = fixture(); c.prepare(chart, observed)
                if field == 'axis': chart['series'][0]['axis'] = value
                else: chart[field] = value
                self.assertEqual(list(c.point_views(chart)), [])

    def test_native_cache_uses_package_relation_even_when_sibling_is_pending(self):
        chart = {'id': 'docx-chart1', 'method': 'docx_chart_cache', 'source_part': 'word/charts/chart1.xml',
                 'title': 'Revenue', 'status': 'pending', 'axes': [{'id': '10', 'label': 'Year'}],
                 'series': [{'name': 'Sales', 'axis_ids': ['10'], 'points': [
                     {'id': 'p1', 'label': '2024', 'value': '120', 'value_source': 'native_cache', 'status': 'confirmed', 'source_part': 'word/charts/chart1.xml/series[0]/point[0]'},
                     {'id': 'p2', 'label': None, 'value': '80', 'value_source': 'native_cache', 'status': 'pending'}]}]}
        views = list(c.point_views(chart)); self.assertEqual(len(views), 1)
        self.assertEqual(views[0]['source_binding']['axis_ids'], ['10'])
        self.assertIn('单位未标明', views[0]['text'])
        self.assertNotIn('observation_id', views[0]['source_binding'])

    def test_human_review_requires_named_timed_confirmation_and_freezes_metadata(self):
        chart, _ = fixture(); point = points(chart)[0]
        point.update(status='confirmed', value_source='human_review', human_reviewed=True,
                     verification='human_reviewed', reviewed_by='reviewer', reviewed_at='2026-10-16T10:00:00+08:00')
        views = list(c.point_views(chart)); self.assertEqual(len(views), 1)
        self.assertEqual(views[0]['source_binding']['reviewed_by'], 'reviewer')
        chart['unit'] = 'USD'; self.assertEqual(list(c.point_views(chart)), [])
        chart['unit'] = 'kCNY'; point.pop('reviewed_by'); self.assertEqual(list(c.point_views(chart)), [])

    def test_human_pie_and_donut_review_accepts_empty_axes_with_audited_point(self):
        for kind in ('pie', 'donut', 'pieChart', 'doughnutChart'):
            with self.subTest(kind=kind):
                chart, _ = fixture(); chart.update(kind=kind, x_label='', y_label='')
                point = points(chart)[0]
                point.update(status='confirmed', value_source='human_review', human_reviewed=True,
                             verification='human_reviewed', reviewed_by='reviewer', reviewed_at='2026-10-16T10:00:00+08:00')
                views = list(c.point_views(chart)); self.assertEqual(len(views), 1)
                self.assertEqual(views[0]['source_binding']['x_label_text'], '')
                self.assertEqual(views[0]['source_binding']['y_label_text'], '')
                self.assertEqual(views[0]['source_binding']['reviewed_by'], 'reviewer')

    def test_human_pie_and_donut_review_rejects_invented_axis_names(self):
        for kind in ('pie', 'donut', 'pieChart', 'doughnutChart'):
            for x_label, y_label in [('Category', ''), ('', 'Share'), ('Category', 'Share')]:
                with self.subTest(kind=kind, x_label=x_label, y_label=y_label):
                    chart, _ = fixture(); chart.update(kind=kind, x_label=x_label, y_label=y_label)
                    point = points(chart)[0]
                    point.update(status='confirmed', value_source='human_review', human_reviewed=True,
                                 verification='human_reviewed', reviewed_by='reviewer', reviewed_at='2026-10-16T10:00:00+08:00')
                    self.assertEqual(list(c.point_views(chart)), [])

    def test_nonfinite_or_malformed_observations_fail_closed(self):
        for bad in ([0, 0, float('nan'), 1], [0, 0, 2, 1], [1, 1, 0, 0]):
            chart, observed = fixture(); observed[0]['box'] = bad
            with self.assertRaises(ValueError): c.prepare(chart, observed)


if __name__ == '__main__':
    unittest.main()
