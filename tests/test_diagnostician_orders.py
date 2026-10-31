"""Actual sheet titles come from a bounded metadata read, never source dates."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import json
import time

import httpx
import pytest

from app.db import init_db
from app.feishu.sheet_writer import SheetWriter, _digest
from app.models import Batch, Order, State


def add_order(factory, identity, token, sheet, *, cached_title="错误旧标题", period="2026-10"):
    data = {"receiver": "合成客户", "phone": "", "address": "", "sheet_title": cached_title,
            "source_day": "2026-10-01", "source_msgids": [],
            "lines": [{"kind": "rail", "product": "合成扶手", "config": "", "qty": "1米"}]}
    payload = SheetWriter.__new__(SheetWriter).order_to_rows(data, 1)
    checkpoint = {"version": 1, "state": "done", "spreadsheet_token": token, "sheet_id": sheet,
        "range": f"{sheet}!A12:I12", "rows": [12, 12], "seq": 1, "payload": payload,
        "payload_digest": _digest(payload), "data_digest": _digest(data)}
    with factory() as db:
        if db.get(Batch, 1) is None:
            db.add(Batch(id=1, status="done"))
        db.add(Order(id=identity, batch_id=1, status="written", data_json=json.dumps(data),
                     written_rows='[12,12]', write_checkpoint_json=json.dumps(checkpoint)))
        db.commit()


@pytest.fixture
def fixture(tmp_path, offline):
    factory = init_db(tmp_path / "business.sqlite")
    writer = SheetWriter(SimpleNamespace(app_id="app", app_secret="secret"), "folder")
    prefix = "spreadsheet:v2:" + _digest(["app", "test", "folder"])[:32] + ":"
    return factory, writer, prefix


def scoped_cache(factory, prefix, token, period="2026-10"):
    with factory() as db:
        db.merge(State(key=prefix + period, value=token))
        db.commit()


def test_actual_metadata_date_title_is_used_instead_of_order_or_processing_date(fixture):
    from app.diagnostician.orders import OrderLocator, TrustedTargetResolver
    factory, writer, prefix = fixture
    scoped_cache(factory, prefix, "workbook")
    add_order(factory, 1, "workbook", "actual-sheet", cached_title="10.1")
    reads = []
    def metadata(client, token):
        reads.append(token)
        return {"sheets": [{"sheet_id": "actual-sheet", "title": "10.27"}]}
    provider = TrustedTargetResolver(factory, writer, metadata_fetcher=metadata)
    locator = OrderLocator(factory, provider)
    result = locator.locate_orders({"sheet": "10.27", "physical_row": 12, "column": "E"}, SimpleNamespace(target_id="test"))
    assert result["status"] == "ok"
    assert result["data"]["sheet_title"] == "10.27"
    assert result["data"]["physical_cell"] == "E12"
    assert reads == ["workbook"]
    assert locator.locate_orders({"sheet": "10.1", "column": "E"}, SimpleNamespace(target_id="test"))["status"] == "needs_input"
    assert reads == ["workbook"]


def test_failed_metadata_is_cached_unknown_and_never_guesses_date_or_creates_sheet(fixture):
    from app.diagnostician.orders import OrderLocator, TrustedTargetResolver
    factory, writer, prefix = fixture
    scoped_cache(factory, prefix, "workbook")
    add_order(factory, 1, "workbook", "opaque-id", cached_title="10.27")
    reads = []
    def metadata(client, token):
        reads.append(token)
        raise TimeoutError("synthetic timeout")
    locator = OrderLocator(factory, TrustedTargetResolver(factory, writer, metadata_fetcher=metadata))
    ctx = SimpleNamespace(target_id="test")
    result = locator.locate_orders({"sheet": "10.27", "physical_row": 12, "column": "E"}, ctx)
    assert result["status"] == "needs_input"
    assert result["data"]["metadata_unknown"] is True
    precise = locator.locate_orders({"order_id": 1, "column": "E"}, ctx)
    assert precise["status"] == "ok"
    assert precise["data"]["sheet_title"] is None
    assert reads == ["workbook"]


def test_same_sheet_id_and_date_in_different_workbooks_stay_ambiguous(fixture):
    from app.diagnostician.orders import OrderLocator, TrustedTargetResolver
    factory, writer, prefix = fixture
    scoped_cache(factory, prefix, "book-a", "2025-10")
    scoped_cache(factory, prefix, "book-b", "2026-10")
    add_order(factory, 1, "book-a", "same-id")
    add_order(factory, 2, "book-b", "same-id")
    reads = []
    def metadata(client, token):
        reads.append(token)
        return {"sheets": [{"sheet_id": "same-id", "title": "10.27"}]}
    locator = OrderLocator(factory, TrustedTargetResolver(factory, writer, metadata_fetcher=metadata))
    ctx = SimpleNamespace(target_id="test")
    first = locator.locate_orders({"sheet": "10.27", "physical_row": 12, "column": "E"}, ctx)
    assert first["status"] == "needs_input"  # Unknown second book cannot make the first falsely unique.
    assert len(reads) == 1
    second = locator.locate_orders({"sheet": "10.27", "physical_row": 12, "column": "E"}, ctx)
    assert second["status"] == "needs_input"
    assert {candidate["order_id"] for candidate in second["data"]["candidates"]} == {1, 2}
    assert reads == ["book-a", "book-b"]


def test_legacy_or_other_writer_cache_does_not_authorize_metadata_reads(fixture):
    from app.diagnostician.orders import trusted_targets
    factory, writer, prefix = fixture
    with factory() as db:
        db.add(State(key="spreadsheet:2026-10", value="legacy"))
        db.add(State(key="spreadsheet:v2:other-app:2026-10", value="other"))
        db.commit()
    add_order(factory, 1, "legacy", "s1")
    add_order(factory, 2, "other", "s2")
    reads = []
    targets = trusted_targets(factory, writer, metadata_fetcher=lambda client, token: reads.append(token))
    assert targets["test"]["sheets"] == {}
    assert reads == []


def test_metadata_cache_is_bounded_expiring_and_coalesces_concurrent_reads(fixture):
    from app.diagnostician.orders import SheetMetadataCache
    _, writer, _ = fixture
    clock = [0.0]
    cache = SheetMetadataCache(ttl=1, max_entries=2, clock=lambda: clock[0])
    reads = []
    def metadata(client, token):
        reads.append(token)
        return {"sheets": [{"sheet_id": "s", "title": "10.27"}]}
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: cache.lookup(writer, "same", fetcher=metadata), range(4)))
    assert all(result["status"] == "ok" for result in results)
    assert reads == ["same"]
    cache.lookup(writer, "other-1", fetcher=metadata)
    cache.lookup(writer, "other-2", fetcher=metadata)
    assert len(cache) == 2
    clock[0] = 2
    cache.lookup(writer, "other-2", fetcher=metadata)
    assert reads.count("other-2") == 2


def test_bounded_client_uses_one_readonly_metadata_request_and_no_retry(fixture, monkeypatch):
    from app.diagnostician.network import BoundedFeishu
    from app.diagnostician.orders import trusted_targets
    factory, writer, prefix = fixture
    scoped_cache(factory, prefix, "book")
    add_order(factory, 1, "book", "s")
    requests = []
    def transport(request):
        requests.append((request.method, request.url.path))
        return httpx.Response(429, json={"code": 429})
    writer.fs = BoundedFeishu("app", "secret", transport=httpx.MockTransport(transport))
    writer.fs._token, writer.fs._token_expire = "token", time.time() + 100
    monkeypatch.setattr("app.test_runtime.check_request", lambda *args: None)
    targets = trusted_targets(factory, writer)
    assert all(sheet["title"] is None for sheet in targets["test"]["sheets"].values())
    assert requests == [("GET", "/open-apis/sheets/v3/spreadsheets/book/sheets/query")]


def test_duplicate_metadata_id_is_unknown_and_fixed_target_does_not_adopt_other_tables(fixture):
    from app.diagnostician.orders import OrderLocator, TrustedTargetResolver
    factory, writer, _ = fixture
    writer.fixed_target = ("fixed-book", "fixed-sheet")
    add_order(factory, 1, "fixed-book", "fixed-sheet")
    add_order(factory, 2, "other-book", "fixed-sheet")
    reads = []
    def metadata(client, token):
        reads.append(token)
        return {"sheets": [{"sheet_id": "fixed-sheet", "title": "10.27"},
                           {"sheet_id": "fixed-sheet", "title": "10.28"}]}
    locator = OrderLocator(factory, TrustedTargetResolver(factory, writer, metadata_fetcher=metadata))
    ctx = SimpleNamespace(target_id="test")
    by_date = locator.locate_orders({"sheet": "10.27", "column": "E"}, ctx)
    assert by_date["status"] == "needs_input"
    assert by_date["data"]["metadata_unknown"] is True
    exact = locator.locate_orders({"sheet": "fixed-sheet", "column": "E"}, ctx)
    assert exact["status"] == "ok"
    assert exact["data"]["order_id"] == 1
    assert reads == ["fixed-book"]
