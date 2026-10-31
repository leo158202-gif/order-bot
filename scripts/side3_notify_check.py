"""Read-only SIDE3 sheet/conflict snapshot and REAL scheduled-notice check.

No app runtime entry/init_db, no message sending, no sheet mutations.
Authentication POST only obtains a token; every business request is GET.
Outputs aggregates/booleans only, never remote values, chat IDs or credentials.
"""
import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
import re
import sqlite3
import sys
from urllib.parse import urlsplit, parse_qs
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import yaml
from app.feishu.client import Feishu, FeishuError
from app.test_runtime import TEST_SPREADSHEET, _policy

TZ = ZoneInfo('Asia/Shanghai')


class ReadOnlyFeishu(Feishu):
    def __init__(self, cfg, chat):
        super().__init__(cfg['app_id'], cfg['app_secret'])
        self.chat = chat

    def req(self, method, path, **kwargs):
        allowed = path.startswith(f'/sheets/v2/spreadsheets/{TEST_SPREADSHEET}/values/') or (
            path == f'/sheets/v3/spreadsheets/{TEST_SPREADSHEET}/sheets/query')
        allowed |= (path == '/im/v1/messages' and
                    kwargs.get('params', {}).get('container_id') == self.chat and
                    kwargs['params'].get('container_id_type') == 'chat')
        if method != 'GET' or not allowed:
            raise ValueError('read-only test-target allowlist rejected request')
        return super().req(method, path, **kwargs)


def rows(fs, sheet, last):
    return fs.req('GET', f'/sheets/v2/spreadsheets/{TEST_SPREADSHEET}/values/'
                  f'{sheet}!A1:I{last}').get('valueRange', {}).get('values', [])


def snapshot(db, fs):
    sheets = fs.req('GET', f'/sheets/v3/spreadsheets/{TEST_SPREADSHEET}/sheets/query')['sheets']
    data, report = {}, []
    for sheet in sheets:
        sid = sheet['sheet_id']
        values = rows(fs, sid, sheet['grid_properties']['row_count'])
        data[sid] = values
        pending = 0
        orders = 0
        for row in values[1:]:
            if row and row[0] not in ('', None):
                orders += 1
                if len(row) > 8 and row[8]:
                    pending += 1
        report.append({'sheet_title': sheet['title'], 'orders': orders,
                       'pending': pending})
    conflicts = []
    for order in db.execute("SELECT id,batch_id,status,written_rows,write_checkpoint_json FROM orders "
                            "WHERE write_checkpoint_json != '{}' ORDER BY id"):
        cp = json.loads(order['write_checkpoint_json'] or '{}')
        if not cp:
            continue
        allowed = cp.get('spreadsheet_token') == TEST_SPREADSHEET
        item = {'order_id': order['id'], 'batch_id': order['batch_id'],
                'state': cp.get('state'), 'rows': cp.get('rows'), 'test_target': allowed}
        if allowed and cp.get('sheet_id') in data:
            start, end = cp['rows']
            actual = data[cp['sheet_id']][start-1:end]
            actual = [[('' if v is None else v) for v in r] + ['']*(9-len(r)) for r in actual]
            actual += [['']*9 for _ in range(end-start+1-len(actual))]
            payload = cp.get('payload', [])
            item.update({'payload_equal': actual == payload,
                         'actual_nonempty': any(v != '' for r in actual for v in r),
                         'different_columns': sorted({chr(65+i) for a,b in zip(actual,payload)
                                                     for i,(x,y) in enumerate(zip(a,b)) if x != y})})
        conflicts.append(item)
    old_targets = db.execute("SELECT count(*) FROM batches WHERE spreadsheet_token != '' "
                             "AND spreadsheet_token != ?", (TEST_SPREADSHEET,)).fetchone()[0]
    completed = [c for c in conflicts if c['state']=='done']
    return {'sheets': report, 'pending_total': sum(s['pending'] for s in report),
            'unfinished_write_checkpoints': [c for c in conflicts if c['state']!='done'],
            'completed_checkpoint_count': len(completed),
            'completed_checkpoint_payload_mismatch_count': sum(c.get('payload_equal') is False for c in completed),
            'legacy_target_batch_count': old_targets,
            'legacy_targets_accessed': False}


def check_real(db, fs, cfg, scheduled_at):
    when = datetime.fromisoformat(scheduled_at)
    if when.tzinfo is None:
        when = when.replace(tzinfo=TZ)
    when = when.astimezone(TZ)
    schedule = db.execute('SELECT * FROM schedules WHERE enabled=1 AND send_at=?',
                          (when.strftime('%H:%M'),)).fetchone()
    if not schedule or when.second or when.microsecond:
        raise ValueError('time must match an enabled real schedule')
    if datetime.now(TZ) < when:
        return {'result': 'WAIT_REAL_TRIGGER', 'scheduled_at': when.isoformat()}
    base = when - timedelta(days=schedule['send_day_offset'])
    def boundary(hhmm):
        if hhmm == '24:00':
            return base.replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days=1)
        h,m=map(int,hhmm.split(':'))
        return base.replace(hour=h,minute=m,second=0,microsecond=0)
    start,end = [int(boundary(schedule[k]).timestamp()*1000) for k in ('window_start','window_end')]
    batch = db.execute("SELECT * FROM batches WHERE kind='scheduled' AND window_start=? "
                       "AND window_end=? ORDER BY id DESC LIMIT 1", (start,end)).fetchone()
    if batch is None:
        return {'result':'WAIT_OR_FAILED_NO_BATCH'}
    cp = json.loads(batch['recovery_json'] or '{}')
    notice = cp.get('notification', {})
    if batch['status'] != 'done':
        return {'result':'NOT_ACCEPTED_BATCH_INCOMPLETE', 'batch_id':batch['id'], 'step':batch['step']}
    if cp.get('resend'):
        return {'result':'NOT_ACCEPTED_CATCHUP_INSTEAD_OF_CRON', 'batch_id':batch['id']}
    count = batch['order_count']
    if count == 0 and not cfg.get('batch',{}).get('notify_when_empty',False):
        return {'result':'EMPTY_NOTICE_DISABLED_NOT_CONTENT_ACCEPTANCE', 'batch_id':batch['id']}
    text = notice.get('text','')
    urls = re.findall(r'https://[^\s]+',text)
    expected = {(v.get('spreadsheet'),v.get('sheet')) for v in cp.get('daily_outputs',{}).values()}
    if not expected:
        expected = {(batch['spreadsheet_token'],batch['sheet_id'])}
    actual = set()
    for url in urls:
        u=urlsplit(url)
        actual.add((u.path.removeprefix('/sheets/'),parse_qs(u.query).get('sheet',[''])[0]))
    domain = cp.get('target',{}).get('domain') or 'feishu.cn'
    # 空批：从未写过表→「本批无订单/未写入表格」且无链；已有当日表→须附完整有效链接（禁残缺）。
    broken = 'https://feishu.cn/sheets/?sheet=' in text
    if count == 0:
        never_written = ('本批无订单' in text and '未写入表格' in text and not urls and not broken)
        existing_linked = (bool(urls) and not broken and '未写入表格' not in text
                           and all(t == TEST_SPREADSHEET and s for t, s in actual)
                           and all(urlsplit(u).netloc == domain for u in urls))
        valid = never_written or existing_linked
        if existing_linked:
            metadata = fs.req('GET', f'/sheets/v3/spreadsheets/{TEST_SPREADSHEET}/sheets/query')['sheets']
            titles = {s['sheet_id']: s['title'] for s in metadata}
            valid = all(s in titles for _, s in actual)
    else:
        valid = (not broken and actual == expected
                 and all(t == TEST_SPREADSHEET and s for t, s in actual)
                 and all(urlsplit(u).netloc == domain for u in urls))
        if valid:
            metadata = fs.req('GET', f'/sheets/v3/spreadsheets/{TEST_SPREADSHEET}/sheets/query')['sheets']
            titles = {s['sheet_id']: s['title'] for s in metadata}
            valid = all(s in titles for _, s in expected)
            for day, out in cp.get('daily_outputs', {}).items():
                d = datetime.fromisoformat(day)
                valid &= titles.get(out['sheet']) == f'{d.month}.{d.day}'
    found = False
    params = {'container_id_type':'chat','container_id':fs.chat,
              'start_time':str(int(when.timestamp())),
              'end_time':str(int((when+timedelta(minutes=30)).timestamp())),
              'page_size':50,'sort_type':'ByCreateTimeAsc'}
    for _ in range(20):
        response=fs.req('GET','/im/v1/messages',params=params)
        for msg in response.get('items',[]):
            try:
                content=json.loads(msg.get('body',{}).get('content','{}')).get('text')
            except (TypeError,ValueError):
                continue
            if (content == text and msg.get('sender',{}).get('sender_type') == 'app'
                    and msg.get('sender',{}).get('id') == fs.app_id and not msg.get('deleted')):
                found=True
        if not response.get('has_more'):
            break
        params['page_token']=response['page_token']
    return {'result':'PASS_REAL_NOTICE' if valid and found and notice.get('state')=='sent'
            else 'NOT_ACCEPTED', 'batch_id':batch['id'], 'order_count':count,
            'content_and_links_valid':valid, 'actual_test_group_bot_message_found':found,
            'receipt_state':notice.get('state'), 'scheduled_at':when.isoformat()}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',type=Path,default=ROOT/'data/test-runtime-20261023')
    parser.add_argument('--scheduled-at',help='REAL schedule ISO time, e.g. 2026-10-25T01:00:00+08:00')
    parser.add_argument('--snapshot',action='store_true',help='GET only: test-sheet pending totals and old checkpoints')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    work,policy=_policy(args.workspace)
    cfg=yaml.safe_load((work/'data/config.yaml').read_text('utf-8'))
    fs=ReadOnlyFeishu(cfg['feishu'],policy['chat'])
    db=sqlite3.connect((work/'data/db.sqlite').as_uri()+'?mode=ro',uri=True)
    db.row_factory=sqlite3.Row
    report={'at':datetime.now(TZ).isoformat(),'read_only':True}
    try:
        if args.snapshot:
            report['snapshot']=snapshot(db,fs)
        if args.scheduled_at:
            report['scheduled_notice']=check_real(db,fs,cfg,args.scheduled_at)
    except FeishuError as exc:
        report['error']={'kind':'FeishuError','code':exc.code,'accepted':False}
    except Exception as exc:
        # Exception messages may contain remote paths or values; never print them.
        report['error']={'kind':type(exc).__name__,'accepted':False}
    finally:
        db.close()
    output=json.dumps(report,ensure_ascii=False,indent=2)
    if args.output:
        args.output.write_text(output,encoding='utf-8')
    print(output)
    if 'error' in report:
        return 2
    result = report.get('scheduled_notice',{}).get('result')
    if result == 'WAIT_REAL_TRIGGER':
        return 3
    if result and result != 'PASS_REAL_NOTICE':
        return 1
    return 0


if __name__=='__main__':
    raise SystemExit(main())
