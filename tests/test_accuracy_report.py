from scripts.replay_gold import compare_orders


def order(**changes):
    return dict(receiver='测试', phone='19900000004', address='测试路1号',
                lines=[dict(product='扶手', config='1支4米', qty='4米')], **changes)


def test_missing_and_extra_stay_in_accuracy_denominator():
    result = compare_orders([order(), dict(order(), phone='19900000005')],
                            [order(), dict(order(), phone='19900000006')])
    a = result['accuracy']
    assert a['correct_orders'] == 1
    assert a['candidate_denominator'] == 3
    assert a['accuracy_percent'] is None
    assert a['diagnostic_candidate_percent'] == 33.33
    assert a['unmatched_gold'] == 1 and a['unmatched_actual'] == 1


def test_logistics_difference_and_review_cannot_count_as_whole_order_correct():
    for actual in [order(logistics='到付'), order(review_reasons=['缺证据'])]:
        a = compare_orders([order()], [actual])['accuracy']
        assert a['correct_orders'] == 0
        assert a['accuracy_percent'] is None
        assert a['diagnostic_candidate_percent'] == 0


def test_empty_and_incomplete_evidence_never_certify_98():
    a = compare_orders([], [])['accuracy']
    assert a['accuracy_percent'] is None
    a = compare_orders([order()], [order()])['accuracy']
    assert a['accuracy_percent'] is None
    assert a['diagnostic_candidate_percent'] == 100
    assert a['acceptance'] == '未验收：范围、实际表回读及独立新样本证据未齐'


def test_window_also_displays_accuracy_without_repairing_contacts():
    from scripts.pairing_window import compare_window
    a = order(source_msgids=['a'])
    result = compare_window({'2026-10-24':[a]}, {'2026-10-24':[a]},
                            messages={'a':dict(roomid='r',msgtime=0,content='本地')})
    assert result['accuracy']['correct_orders'] == 0
    assert result['accuracy']['source_date_unverified'] == 1
    assert result['daily_accuracy']['2026-10-24']['accuracy_percent'] is None


def test_acceptance_98_window_excludes_pre_24_history_from_both_sides():
    from scripts.pairing_window import compare_window
    old = order(source_msgids=['old'])
    new = dict(order(source_msgids=['new']), phone='19900000005')
    r = compare_window({'2026-10-23':[old], '2026-10-24':[new]},
        {'2026-10-23':[dict(old, lines=[])], '2026-10-24':[new]},
        messages={k:dict(roomid='r',msgtime=timestamp(day),content='本地')
                  for k,day in [('old','2026-10-23'),('new','2026-10-24')]})
    assert r['accuracy']['gold_orders'] == 1
    assert r['accuracy']['correct_orders'] == 1
    assert r['accuracy']['candidate_denominator'] == 1
    assert r['daily_accuracy']['2026-10-23']['accuracy_percent'] is None
    assert r['historical_accuracy']['gold_orders'] == 2


def test_late_gold_sheet_does_not_move_old_source_into_98_window():
    from scripts.pairing_window import compare_window
    a = order(source_msgids=['a'])
    r = compare_window({'2026-10-24':[a]}, {'2026-10-23':[a]},
        messages={'a':dict(roomid='r',msgtime=timestamp('2026-10-23'),content='本地')})
    assert r['accuracy']['candidate_denominator'] == 0
    assert r['daily_accuracy']['2026-10-23']['paired_orders'] == 1
    assert r['accuracy']['accuracy_percent'] is None


def timestamp(day):
    from datetime import datetime
    return int(datetime.fromisoformat(day+'T12:00:00+08:00').timestamp()*1000)


def test_unverified_candidates_are_not_published_as_accuracy():
    from scripts.accuracy_report import accuracy_summary
    a = accuracy_summary(103, 75, 0, 60)
    assert a['accuracy_percent'] is None
    assert a['denominator'] is None
    assert a['candidate_denominator'] == 118
    assert a['diagnostic_candidate_percent'] == 0
    assert a['paired_not_correct'] == 60
    assert not a['observed_target_met']
    b = accuracy_summary(2, 2, 1, 1, scope_verified=True)
    assert b['accuracy_percent'] == 33.33 and b['denominator'] == 3
    assert b['official_accuracy_percent'] is None


def test_window_uses_original_message_date_not_supplied_sheet_day():
    from scripts.pairing_window import compare_window
    a = order(source_msgids=['a'])
    r = compare_window({'2026-10-24':[a]}, {'2026-10-24':[a]},
        messages={'a':dict(roomid='r',msgtime=timestamp('2026-10-23'),content='本地')})
    assert r['pairs'][0]['source_day'] == '2026-10-23'
    assert r['accuracy']['paired_orders'] == 0
    assert r['accuracy']['candidate_denominator'] == 0


def test_pair_dimensions_expose_logistics_and_review_even_if_products_match():
    from scripts.pairing_window import compare_window
    a = order(source_msgids=['a'], review_reasons=['缺来源证据'])
    g = order(logistics='安能')
    r = compare_window({'2026-10-25':[g]}, {'2026-10-24':[a]},
        messages={'a':dict(roomid='r',msgtime=timestamp('2026-10-24'),content='本地 扶手1支4米')})
    checks = r['pairs'][0]['checks']
    assert checks == dict(products_equal=True, contacts_equal=True,
                         logistics_equal=False, review_clear=False)
    assert r['accuracy']['paired_dimensions']['logistics_different'] == 1
    assert r['accuracy']['paired_dimensions']['review_pending'] == 1
    assert r['accuracy']['accuracy_percent'] is None


def test_piece_length_separator_is_format_only_but_arithmetic_is_not():
    from scripts.replay_gold import comparison_lines
    expected = order()
    expected['lines'][0]['config'] = '2支4米1支3米'
    for separator in ['+', '＋', '，']:
        actual = order()
        actual['lines'][0]['config'] = '2支4米'+separator+'1支3米'
        assert comparison_lines(expected) == comparison_lines(actual)
    actual['lines'][0]['config'] = '2支4米+3米'
    assert comparison_lines(expected) != comparison_lines(actual)
