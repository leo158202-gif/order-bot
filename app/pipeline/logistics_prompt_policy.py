"""Owner layout contract above signed historical prompts/rules; no disk migration."""
import re

LEGACY_LOGISTICS_INSTRUCTION = '- 自提、代收金额、到付按金标准写H物流列logistics，仅保留本单原文明示要求，不猜承运商或金额；不写本地或店名。旧进notes备注规则作废。'
CORE_LOGISTICS_INSTRUCTION = '- H列物流始终为空；全部明确物流信息并入G列地址，保留原地址和本单原文明示物流，不重复追加，不猜承运商或金额；logistics必须为空字符串。'
FINAL_LAYOUT_CONSTRAINT = ('\n\n最高优先用户布局约束（覆盖旧金标、规则、已发布提示和学习样例中的冲突指令）：'
    'H列物流始终为空；全部明确物流信息并入G列地址，保留原地址和信息、不重复追加，不凭金标或默认值猜写；'
    '承运商、自提、到付/现付、代收金额、取件/配送均遵守。logistics必须为空字符串。')


def remove_legacy_H_guidance(text):
    """Suppress obsolete H guidance in effective text, retain archived originals."""
    lines = []
    for line in text.splitlines(keepends=True):
        # H头 is a product term. Only explicit column destinations are policy.
        clauses = re.split(r'([；;])', line)
        for index in range(0, len(clauses), 2):
            clause = clauses[index]
            mentions = re.search(r'H列|H物流|(?:落|写|填)H(?=列|物流|[\s"\'，。；;]|$)|(?:write|fill)\s+(?:logistics\s+)?(?:to|in|into)\s+H\b', clause, re.I)
            blank = re.search(r'H列.*(?:留空|为空)|H.*(?:always empty|blank)', clause, re.I)
            if mentions and not blank:
                ending = '\r\n' if clause.endswith('\r\n') else '\n' if clause.endswith('\n') else ''
                clauses[index] = '# 已撤销旧物流布局指令，H为空，明确物流随G地址保留。' + ending
        lines.append(''.join(clauses))
    return ''.join(lines)


def compatible_prompt_core(text, default):
    """Only the exact withdrawn core sentence can migrate after signature check."""
    if text.startswith(default):
        return text
    migrated = text.replace(LEGACY_LOGISTICS_INSTRUCTION, CORE_LOGISTICS_INSTRUCTION, 1)
    if migrated.startswith(default):
        return migrated
    raise ValueError('提示词核心约束与当前程序不一致，须重新验证')
