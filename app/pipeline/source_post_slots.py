"""仅纠正同一原文、同一规格、总支数不变的立柱槽型数量互换。"""
import re
from collections import defaultdict


def reconcile_slots(order):
    groups = defaultdict(list)
    product = ''
    for line in order.get('lines', []):
        if line.get('product'):
            product = line['product']
        if line.get('kind') != 'post':
            continue
        match = re.fullmatch(r'(.*?)(单槽|双槽|转角|三槽|无槽)', line.get('config', '').strip())
        if match and line.get('raw'):
            groups[(line['raw'], product, match[1].strip())].append((line, match[2]))
    for (raw, _, _), rows in groups.items():
        if len(rows) < 2 or len({slot for _, slot in rows}) != len(rows):
            continue
        tokens = re.findall(r'(?<![\d.])(\d+)\s*(?:支|只)?\s*(单槽|双槽|转角|三槽|无槽|单|双|转)(?![\u4e00-\u9fff])', raw)
        aliases = {'单': '单槽', '双': '双槽', '转': '转角'}
        slots = [aliases.get(s, s) for _, s in tokens]
        if len(slots) != len(set(slots)) or set(slots) != {s for _, s in rows}:
            continue
        counts = {aliases.get(s, s): int(n) for n, s in tokens}
        quantities = [re.fullmatch(r'(\d+)(支|只)', line.get('qty', '')) for line, _ in rows]
        if not all(quantities) or sum(int(q[1]) for q in quantities) != sum(counts.values()):
            continue
        for (line, slot), quantity in zip(rows, quantities):
            corrected = str(counts[slot]) + quantity[2]
            if corrected != line['qty']:
                line['qty'] = corrected
                line.setdefault('fixes', []).append('source:post_slot_counts')
