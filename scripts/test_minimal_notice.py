"""只向隔离目录登记的测试群测发一条五项通知；结果不明禁止重发。"""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import yaml
from app.test_runtime import enter, TEST_SPREADSHEET
from app.feishu.client import Feishu
from app.feishu.notifier import build_message


def readback_test_table(fs):
    """只读指定测试表的两日订单及现有备注表头，不创建或修正任何单元格。"""
    from scripts.publish_test_replay import remote_orders
    sheets = fs.req('GET', f'/sheets/v3/spreadsheets/{TEST_SPREADSHEET}/sheets/query', _retries=1)['sheets']
    result = {}
    for sheet in sheets:
        if sheet['title'] not in ('10.21', '10.22'):
            continue
        sid = sheet['sheet_id']
        header = fs.req('GET', f'/sheets/v2/spreadsheets/{TEST_SPREADSHEET}/values/{sid}!A1:AZ1', _retries=1).get('valueRange', {}).get('values', [])
        values = fs.req('GET', f'/sheets/v2/spreadsheets/{TEST_SPREADSHEET}/values/{sid}!A1:I10000', _retries=1).get('valueRange', {}).get('values', [])
        records = remote_orders(values)
        result[sheet['title']] = {'orders': len(records), 'rows': len(values),
                                  'pending': sum(bool(r['values'][0][8]) for r in records),
                                  'notes_columns': [i+1 for i, value in enumerate(header[0] if header else []) if value == '备注'],
                                  'review_header': header[0][8] if header and len(header[0]) > 8 else None}
    if set(result) != {'10.21', '10.22'}:
        raise ValueError('测试表缺少10.21或10.22，回读未完成')
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--workspace', type=Path, required=True)
    p.add_argument('--receipt-name', default='minimal-notice-receipt.json')
    p.add_argument('--readback', action='store_true')
    args = p.parse_args()
    enter(args.workspace)
    if Path(args.receipt_name).name != args.receipt_name or not args.receipt_name.endswith('.json'):
        raise ValueError('回执须为隔离data目录内的JSON文件名')
    receipt = Path('data') / args.receipt_name
    if receipt.exists():
        raise RuntimeError('本轮测发已有记录，不重复发送')
    cfg = yaml.safe_load(Path('data/config.yaml').read_text(encoding='utf-8'))['feishu']
    text = build_message('manual', '', 0, 0,
                         f'https://{cfg.get("tenant_domain") or "feishu.cn"}/sheets/{TEST_SPREADSHEET}', '', 0, 0)
    state = {'state': 'prepared', 'text': text, 'test_only': True}
    def save():
        receipt.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    save()
    fs = Feishu(cfg['app_id'], cfg['app_secret'])
    try:
        fs.token  # 鉴权先于发送意图；鉴权失败可证明尚未发消息。
    except Exception as error:
        state.update(state='not_sent', error_type=type(error).__name__, reason='鉴权连接失败，消息未提交')
        save()
        print('测试通知未发送：鉴权连接失败，已记录，不重复尝试')
        return 1
    if args.readback:
        try:
            state['readback'] = readback_test_table(fs)
            state['readback_state'] = 'done'
        except Exception as error:
            state.update(readback_state='failed', readback_error_type=type(error).__name__,
                         readback_error_code=getattr(error, 'code', None))
        save()
    state['state'] = 'inflight'
    save()
    try:
        data = fs.req('POST', '/im/v1/messages', _retries=1, params={'receive_id_type': 'chat_id'},
                      json={'receive_id': cfg['test']['chat_id'], 'msg_type': 'text',
                            'content': json.dumps({'text': text}, ensure_ascii=False)})
    except Exception as error:
        state.update(state='unknown', error_type=type(error).__name__, error_code=getattr(error, 'code', None),
                     reason='发送未取得成功回执，结果未确认，不重复发送')
        save()
        print('测试通知未取得成功回执；已记录，不重复发送')
        return 1
    state.update(state='sent', message_id=data.get('message_id'))
    save()
    print('测试群五项通知已发送；回执已保存')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
