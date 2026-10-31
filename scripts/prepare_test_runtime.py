"""准备独立测试副本；原库URI只读备份，不改原配置或规则。"""
from pathlib import Path
from datetime import datetime,timezone
import argparse
import json
import sqlite3
import sys

import yaml

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.test_runtime import TEST_SPREADSHEET


def prepare_runtime(root,work):
    root=Path(root).resolve();work=Path(work).resolve()
    source=root/'data/db.sqlite'
    if work.exists() or work==root or source.is_relative_to(work):
        raise ValueError('测试目录必须为新的独立目录，不覆盖现有目录')
    cfg=yaml.safe_load((root/'data/config.yaml').read_text(encoding='utf-8'))
    if cfg.get('target')!='test' or not cfg['feishu']['test'].get('chat_id'):
        raise ValueError('原配置不是完整测试目标，未准备副本')
    with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as original:
        original.execute('PRAGMA query_only=ON')
        cached=original.execute("SELECT value FROM state WHERE key='spreadsheet_token:2026-10'").fetchone()
        if not cached or cached[0]!=TEST_SPREADSHEET:
            raise ValueError('当前月缓存不是获准测试表，未准备副本')
        (work/'data').mkdir(parents=True)
        with sqlite3.connect(work/'data/db.sqlite') as destination:original.backup(destination)
    cfg['target']='test'
    cfg['feishu']={k:v for k,v in cfg['feishu'].items() if not isinstance(v,dict) or k=='test'}
    cfg['wecom']['private_keys']={version:str((Path(p) if Path(p).is_absolute() else root/'data'/p).resolve())
                                  for version,p in cfg['wecom']['private_keys'].items()}
    (work/'data/config.yaml').write_text(yaml.safe_dump(cfg,allow_unicode=True),encoding='utf-8')
    (work/'data/rules.yaml').write_bytes((root/'rules/rules.yaml').read_bytes())
    manifest={'source_db':str(source),'spreadsheet':TEST_SPREADSHEET,'chat':cfg['feishu']['test']['chat_id'],
              'prepared_at':datetime.now(timezone.utc).isoformat()}
    (work/'data/test_runtime.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    return work


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',required=True,type=Path)
    args=parser.parse_args()
    try:
        work=prepare_runtime(ROOT,args.workspace)
        print('已准备隔离测试目录：'+str(work)+'；原库与原配置未修改')
        return 0
    except (ValueError,OSError,KeyError,sqlite3.Error,yaml.YAMLError):
        print('测试副本准备失败，未启动或发送；请核查目录和测试配置',file=sys.stderr)
        return 1


if __name__=='__main__':raise SystemExit(main())
