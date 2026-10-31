"""获准真机测试的隔离边界：原库只读副本、固定测试表及测试群。"""
from pathlib import Path
import json
import os
import re
import sys
from urllib.parse import urlsplit, parse_qs, unquote

import yaml

__test__ = False
ENV='ORDER_BOT_TEST_RUNTIME'
TEST_SPREADSHEET='TEST_SPREADSHEET_PLACEHOLDER'
PRODUCTION_DATABASE = Path(__file__).resolve().parents[1] / 'data/db.sqlite'


class TestBoundaryError(RuntimeError):
    pass


def install_sqlite_guard():
    """测试收集前拦截直接sqlite/SQLAlchemy连接，避免漏过init_db保护。"""
    def audit(event, args):
        if event != 'sqlite3.connect' or str(args[0]) == ':memory:':
            return
        value = str(args[0])
        if value.startswith('file:'):
            uri = urlsplit(value)
            if parse_qs(uri.query).get('mode') == ['ro']:
                return
            value = unquote(uri.path)
            if os.name == 'nt' and re.match(r'^/[A-Za-z]:', value):
                value = value[1:]
        candidate = Path(value).resolve()
        original = PRODUCTION_DATABASE.resolve()
        if candidate == original or (candidate.exists() and original.exists()
                                     and os.path.samefile(candidate, original)):
            raise TestBoundaryError('测试拒绝以可写连接打开生产库')
    sys.addaudithook(audit)


def _policy(work):
    try:
        work=Path(work).resolve(strict=True)
        manifest=json.loads((work/'data/test_runtime.json').read_text(encoding='utf-8'))
        cfg=yaml.safe_load((work/'data/config.yaml').read_text(encoding='utf-8'))
        database=(work/'data/db.sqlite').resolve(strict=True)
        if (database.parent!=work/'data' or os.path.samefile(database,manifest['source_db'])
                or manifest['spreadsheet']!=TEST_SPREADSHEET or not manifest['chat']
                or cfg.get('target')!='test' or cfg['feishu']['test']['chat_id']!=manifest['chat']):
            raise ValueError()
        return work,manifest
    except (OSError,ValueError,TypeError,KeyError,yaml.YAMLError):
        raise TestBoundaryError('测试隔离目录或目标不一致，未执行业务操作') from None


def enter(work):
    work,_=_policy(work)
    os.chdir(work)
    os.environ[ENV]=str(work)


def check_database(path):
    if str(path) == ':memory:':
        return
    candidate = Path(path).resolve()
    original = PRODUCTION_DATABASE.resolve()
    if candidate == original or (candidate.exists() and original.exists()
                                  and os.path.samefile(candidate, original)):
        raise TestBoundaryError('生产库保留 schema v3；禁止初始化、迁移或写入，请使用独立测试库')
    if ENV not in os.environ:
        from app.deployment import check_database as check_customer_database
        check_customer_database(path)
        return
    work,_=_policy(os.environ[ENV])
    if Path(path).resolve()!=work/'data/db.sqlite':
        raise TestBoundaryError('测试运行拒绝访问原运行库或其它数据库')


def check_request(method,path,kwargs,client=None):
    if ENV not in os.environ:
        from app import deployment
        if deployment.active():
            deployment.check_request(client,method,path,kwargs)
            return
        if method.upper() == 'GET':
            return
        raise TestBoundaryError('写入必须先进入指定测试隔离目录；未发送请求')
    _,policy=_policy(os.environ[ENV])
    method=method.upper()
    prefix=re.fullmatch(r'/sheets/v[23]/spreadsheets/([^/]+)/(.*)',path)
    if prefix and prefix[1]==TEST_SPREADSHEET:
        if method=='GET':return
        allowed={'values':'PUT','styles_batch_update':'PUT','dimension_range':'PUT',
                 'merge_cells':'POST','sheets_batch_update':'POST'}
        if allowed.get(prefix[2])==method:
            if prefix[2]=='values':
                destination=kwargs.get('json',{}).get('valueRange',{}).get('range','')
                match=re.fullmatch(r'([^!]+)![A-Z]+(\d+):[A-Z]+(\d+)',destination)
                if not match:raise TestBoundaryError('测试写值范围必须为明确单区间')
                protected=policy.get('protected_sheet_rows',{}).get(match[1],0)
                if int(match[2])<=protected:
                    # R8旧样本例外：单I格清空或预先批准的精确单行B:D值。
                    rows=policy.get('review_clear_rows',{}).get(match[1],[])
                    value=kwargs.get('json',{}).get('valueRange',{})
                    clear=(int(match[2])>1 and int(match[2]) in rows
                            and destination==f'{match[1]}!I{match[2]}:I{match[2]}'
                            and value.get('values')==[['']])
                    patches=policy.get('protected_product_patches',{})
                    product=(int(match[2])>1
                             and destination==f'{match[1]}!B{match[2]}:D{match[2]}'
                             and destination in patches and value.get('values')==patches[destination]
                             and len(value.get('values',[]))==1 and len(value['values'][0])==3)
                    restored=policy.get('review_restore_patches',{}).get(destination)
                    restore=(int(match[2])>1 and destination==f'{match[1]}!I{match[2]}:I{match[2]}'
                             and isinstance(restored,list) and len(restored)==1
                             and isinstance(restored[0],list) and len(restored[0])==1
                             and isinstance(restored[0][0],str) and bool(restored[0][0].strip())
                             and value.get('values')==restored)
                    if not (clear or product or restore):
                        raise TestBoundaryError('历史样本只允许已授权的单格清理或精确商品修正')
            if prefix[2]=='sheets_batch_update':
                from app.feishu.client import is_order_sheet_title
                requests=kwargs.get('json',{}).get('requests',[])
                if (not isinstance(requests,list) or not requests
                        or any(not isinstance(r,dict) or set(r)!={'addSheet'} for r in requests)):
                    raise TestBoundaryError('本轮只允许新增日期工作表，拒绝删除或重命名')
                for request in requests:
                    addition=request['addSheet']
                    properties=addition.get('properties') if isinstance(addition,dict) else None
                    if not isinstance(properties,dict) or not is_order_sheet_title(properties.get('title')):
                        raise TestBoundaryError('订单月表只允许有效的M.D日期工作表；诊断验收必须使用独立工作簿')
            return
        if prefix[2]=='dimension_range' and method=='POST':
            dimension=kwargs.get('json',{}).get('dimension',{})
            if (set(dimension)=={'sheetId','majorDimension','length'} and isinstance(dimension['sheetId'],str)
                    and dimension['majorDimension']=='ROWS' and type(dimension['length']) is int
                    and 0<dimension['length']<=5000):return
    if method=='POST' and path=='/im/v1/messages':
        params=kwargs.get('params',{});body=kwargs.get('json',{})
        if (params.get('receive_id_type')=='chat_id' and body.get('receive_id')==policy['chat']
                and body.get('msg_type')=='text'):return
    raise TestBoundaryError('请求超出指定测试表或测试群，已阻止')
