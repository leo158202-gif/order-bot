"""Test resident: new durable send-point catch-up enabled, old recovery frozen.

Existing schedule enablement remains controlled by stored user settings.
Run from repository root with the usual app.main arguments.
"""
import hashlib
import json
import logging
import marshal
from pathlib import Path
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import main, scheduler, send_events
from app.feishu import sheet_writer
from app.pipeline import batch_runner, normalizer, source_group_guard, rule_engine, media_evidence, order_amendments, segmenter, source_context, source_product_details, gold_rules
from app.scheduler import BatchScheduler
from app.web import routes, admin_routes


def attest():
    from app.test_runtime import ENV, _policy
    import os
    work, policy = _policy(os.environ[ENV])
    database = work / 'data/db.sqlite'
    probes = {}
    try:
        source_group_guard.single_room(
            {'source_msgids': ['a', 'b']},
            {'a': {'roomid': 'one'}, 'b': {'roomid': 'two'}})
    except source_group_guard.SourceGroupError:
        probes['mixed_group_rejected'] = True
    class NoRemote:
        def req(self, *args, **kwargs):
            raise AssertionError('attestation must not access remote services')
    writer = sheet_writer.SheetWriter(NoRemote(), '')
    probes['sandbox_cache_fixed_target'] = (
        writer.get_or_create_spreadsheet(datetime(2026, 10, 24)) == policy['spreadsheet'])
    writer.target_name = 'prod'
    try:
        writer.get_or_create_spreadsheet(datetime(2026, 10, 24))
    except sheet_writer.WriteRecoveryError:
        probes['sandbox_prod_rejected'] = True
    probes['batch_write_guard_loaded'] = 'guard_stored_orders' in batch_runner._write_daily.__code__.co_names
    probes['direct_write_guard_loaded'] = 'guard_stored_orders' in sheet_writer.SheetWriter.write_orders.__code__.co_names
    probes['normalizer_guard_loaded'] = 'validate_group_sources' in normalizer.prepare_result.__code__.co_names
    probes['durable_send_events_loaded'] = 'event_id' in batch_runner.run_batch.__wrapped__.__code__.co_varnames
    probes['safe_catchup_loaded'] = 'enqueue_scheduled' in BatchScheduler.catch_up.__code__.co_names
    probes['legacy_recovery_frozen'] = BatchScheduler.plan_recovery(None) == []
    modules = [main, batch_runner, normalizer, source_group_guard, sheet_writer,
               scheduler, send_events, routes, admin_routes, rule_engine, media_evidence, order_amendments, segmenter,
               source_context, source_product_details, gold_rules]
    from scripts.resident_reload_check import SOURCES
    import importlib
    for name in SOURCES:
        if name.endswith('.py'):
            loaded_module = importlib.import_module(name[:-3].replace('/', '.'))
            if loaded_module not in modules:
                modules.append(loaded_module)
    facts = {
        'pid': os.getpid(), 'at_utc': datetime.now(timezone.utc).isoformat(),
        'cwd': str(Path.cwd()), 'database': str(database),
        'database_differs_from_source': not database.samefile(policy['source_db']),
        'startup_recovery': 'paused_this_process',
        'safe_event_catchup': 'enabled_after_first_pull', 'normal_cron': 'persistent_send_points',
        'probes': probes,
        'files': {str(Path(m.__file__).resolve().relative_to(ROOT)):
                  hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest() for m in modules},
        'loaded_code_sha256': {
            f.__qualname__: hashlib.sha256(marshal.dumps(f.__code__)).hexdigest()
            for f in [batch_runner._write_daily, normalizer.prepare_result,
                      sheet_writer.SheetWriter.write_orders,
                      sheet_writer.SheetWriter.get_or_create_spreadsheet,
                      BatchScheduler.plan_catch_up, BatchScheduler.catch_up,
                      send_events.enqueue_manual, send_events.execute_event.__wrapped__]},
    }
    facts['files'].update({name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                           for name in SOURCES if not name.endswith('.py')})
    if not all(probes.values()):
        raise RuntimeError('SIDE3 runtime attestation failed')
    (work / 'data/side3-loaded.json').write_text(json.dumps(facts, indent=2), encoding='utf-8')
    logging.getLogger('side3.ops').info('SIDE3 in-process guards/cache attested; pid=%d', os.getpid())


if __name__ == '__main__':
    original_logging = main.setup_logging
    def setup_logging():
        original_logging()
        attest()
    main.setup_logging = setup_logging
    main.main(maintenance_enabled=True)
