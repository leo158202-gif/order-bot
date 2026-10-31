"""Conservative comparison display; offline agreement is not live acceptance."""
from decimal import Decimal, ROUND_DOWN

ACCURACY_START_DAY = '2026-10-24'
ACCURACY_START = '2026-10-24T00:00:00+08:00'


def dated_accuracy(summary, source_day):
    """Keep historical diagnostics without letting them certify the 98% cohort."""
    result = dict(summary, source_day=source_day)
    if source_day < ACCURACY_START_DAY:
        result.update(accuracy_percent=None, observed_target_met=False,
                      acceptance='历史对照：不计入24日00:00起的98%验收')
    return result


def whole_order_correct(expected, actual, *, fields_equal, lines_equal):
    return (fields_equal and lines_equal
            and str(expected.get('logistics') or '').strip() == str(actual.get('logistics') or '').strip()
            and not actual.get('review_reasons') and not actual.get('needs_review'))


def comparison_checks(expected, actual, *, fields_equal, lines_equal):
    return dict(products_equal=lines_equal, contacts_equal=fields_equal,
                logistics_equal=str(expected.get('logistics') or '').strip() == str(actual.get('logistics') or '').strip(),
                review_clear=not bool(actual.get('review_reasons') or actual.get('needs_review')))


def dimension_summary(checks):
    checks = list(checks)
    return {key: sum(not c[field] for c in checks) for key, field in (
        ('products_different', 'products_equal'), ('contacts_different', 'contacts_equal'),
        ('logistics_different', 'logistics_equal'), ('review_pending', 'review_clear'))}


def accuracy_summary(gold_total, actual_total, correct, paired, *, gold_available=True, scope_verified=False):
    extra = actual_total - paired
    denominator = gold_total + extra
    rate = (float((Decimal(correct) * 100 / denominator).quantize(Decimal('.01'), rounding=ROUND_DOWN))
            if denominator and gold_available else None)
    return dict(correct_orders=correct, gold_orders=gold_total, actual_orders=actual_total,
                paired_orders=paired, paired_not_correct=paired-correct,
                unmatched_gold=gold_total-paired, unmatched_actual=extra,
                denominator=denominator if scope_verified else None,
                accuracy_percent=rate if scope_verified else None, target_percent=98,
                candidate_denominator=denominator, diagnostic_candidate_percent=rate,
                scope_verified=scope_verified,
                official_accuracy_percent=None,
                acceptance_start=ACCURACY_START,
                pending_or_unverifiable='未核实', rule_excluded='未核实',
                confirmed_missing_rate='未核实', confirmed_extra_rate='未核实',
                observed_target_met=bool(scope_verified and gold_available and denominator and correct*100 >= denominator*98),
                acceptance='未验收：范围、实际表回读及独立新样本证据未齐',
                basis='范围核清后：整单正确/(纳入金标数+我方未配对数)，向下保留两位。范围未核清时分母和正确率为空，候选分母及其诊断比值另列，不能称业务正确率或98%成绩；未配对不等于已证漏单/多登，未知不得删除。',
                evidence='本次输入对照，非实际表整体验收；无金标或零分母显示未验，不显示100%')
