"""阶段 5 验收：管理后台各页面。"""
import json
import re
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.models import Batch, IpHistory, Order, RulesVersion, Schedule, State


@pytest.fixture()
def client(isolated_web_app):
    create_app, factory = isolated_web_app
    return TestClient(create_app()), factory


def test_schedules_page_and_add(client):
    c, factory = client
    assert c.get("/admin/schedules").status_code == 200
    c.post("/admin/schedules", data={"name": "测试批次", "window_start": "08:00",
                                     "window_end": "24:00", "send_at": "01:00",
                                     "send_day_offset": 1})
    s = factory()
    assert s.query(Schedule).filter_by(name="测试批次").count() == 1
    s.close()


def test_rules_page_and_version(client):
    c, factory = client
    assert c.get("/admin/rules").status_code == 200
    base_hash = re.search(r'name="base_hash" value="([a-f0-9]+)"', c.get('/admin/rules').text)[1]
    c.post("/admin/rules", data={"base_hash": base_hash, "yaml_text": open("rules/rules.yaml", encoding="utf-8").read(),
                                 "note": "测试保存"})
    s = factory()
    assert s.query(RulesVersion).count() == 1
    s.close()


def test_rules_bad_yaml_rejected(client):
    c, _ = client
    base_hash = re.search(r'name="base_hash" value="([a-f0-9]+)"', c.get('/admin/rules').text)[1]
    r = c.post("/admin/rules", data={"base_hash": base_hash, "yaml_text": ":\n  - [bad", "note": ""})
    assert "格式错误" in r.text




def _gold_json(entries_a, entries_b):
    """把对照 JSON 写到 admin_routes.DATA 当前指向的 data 目录。"""
    import json as _json
    import app.web.admin_routes as ar
    from pathlib import Path
    payload = {"sheet": "10.21", "class_a": entries_a, "class_b": entries_b}
    p = Path(ar.DATA) / "gold_diff.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _gold_entry(oid, gold_seq="5"):
    return {"order_id": oid, "gold_seq": gold_seq, "receiver": "张三",
            "phone": "19900000009", "gold_receiver": "张三", "gold_phone": "19900000009",
            "diffs": [], "db_lines": [{"product": "A", "config": "c", "qty": "1支"}],
            "gold_lines": [{"product": "A", "config": "c", "qty": "1支"}]}


def test_gold_page_shows_and_approve_a(client):
    """对照页展示 A 类，全部通过后订单转 approved。"""
    c, factory = client
    s = factory()
    o = Order(batch_id=1, status="pending", data_json='{"receiver":"张三","lines":[]}',
              reasons_json='["缺电话"]')
    s.add(o)
    s.commit()
    oid = o.id
    s.close()
    _gold_json([_gold_entry(oid)], [])
    r = c.get("/admin/pending/gold")
    assert r.status_code == 200
    assert "建议放行" in r.text and "张三" in r.text
    c.post("/admin/pending/gold/approve_a", data={"oids": [str(oid)]}, follow_redirects=False)
    s = factory()
    assert s.get(Order, oid).status == "approved"
    s.close()


def test_gold_resolve_b_keep_and_discard(client):
    """B 类：keep 转 approved、discard 转 discarded、gold_rewrite 用金标准覆盖。"""
    c, factory = client
    s = factory()
    ids = []
    for st in ("pending", "pending", "held"):
        o = Order(batch_id=1, status=st,
                  data_json='{"receiver":"x","phone":"111","source_msgids":["m1"],'
                            '"lines":[{"kind":"post","product":"旧产品","config":"c","qty":"1支"}]}',
                  reasons_json='["r"]')
        s.add(o)
        s.commit()
        ids.append(o.id)
    s.close()
    entries = [_gold_entry(i, "9") for i in ids]
    # 第三单（gold_rewrite）的金标准行与库内不同，验证覆盖
    entries[2]["gold_lines"] = [
        {"product": "灰色内卡槽立柱 焊接 H款", "config": "【2.0】850高 单槽", "qty": "46支"},
        {"product": "焊接 新款三槽主体 卡条离地10公分 H头用锁螺丝的", "config": "", "qty": ""},
    ]
    entries[2]["gold_receiver"] = "真实收货人"
    entries[2]["gold_phone"] = "19900000015"
    _gold_json([], entries)
    r = c.get("/admin/pending/gold")
    assert "需人工看" in r.text
    c.post("/admin/pending/gold/resolve_b",
           data={f"action_{ids[0]}": "keep", f"action_{ids[1]}": "discard",
                 f"action_{ids[2]}": "gold_rewrite"}, follow_redirects=False)
    s = factory()
    assert s.get(Order, ids[0]).status == "approved"
    assert s.get(Order, ids[1]).status == "discarded"
    o3 = s.get(Order, ids[2])
    assert o3.status == "approved"
    d = json.loads(o3.data_json)
    # 金标准覆盖了 lines / receiver / phone
    assert d["lines"][0]["product"] == "灰色内卡槽立柱 焊接 H款"
    assert d["lines"][0]["qty"] == "46支"
    assert d["lines"][1]["kind"] == "weld_note"  # 焊接说明行识别正确
    assert d["receiver"] == "真实收货人"
    assert d["phone"] == "19900000015"
    assert d["source_msgids"] == ["m1"]  # 原消息来源保留
    assert d["gold_rewritten_from"] == "9"
    s.close()


def test_gold_page_hides_processed(client):
    """已处理（written/approved/discarded）的订单不再出现在对照页。"""
    c, factory = client
    s = factory()
    o = Order(batch_id=1, status="written", data_json='{"receiver":"旧单","lines":[]}',
              written_rows="[2,3]")
    s.add(o)
    s.commit()
    oid = o.id
    s.close()
    _gold_json([_gold_entry(oid)], [])
    r = c.get("/admin/pending/gold")
    assert "旧单" not in r.text


def test_batches_page(client):
    c, factory = client
    s = factory()
    s.add(Batch(kind="scheduled", window_start=0, window_end=3600000,
                status="done", order_count=5))
    s.commit()
    s.close()
    r = c.get("/admin/batches")
    assert "成功" in r.text


def test_ip_page_and_mark(client, monkeypatch):
    c, factory = client
    import app.web.admin_routes as ar
    probe = Mock(return_value=(True, "offline test"))
    monkeypatch.setattr(ar, "_probe_pull", probe)
    s = factory()
    s.add(State(key="registered_ips", value="183.13.19.47"))
    s.commit()
    s.close()
    response = c.post("/admin/ip/mark_registered", data={"ip": "183.13.19.47"},
                      follow_redirects=False)
    probe.assert_called_once_with()
    assert response.status_code == 303
    assert "msg=restored" in response.headers["location"]
    s = factory()
    assert s.get(IpHistory, "183.13.19.47").registered is True
    s.close()


def test_settings_page(client):
    c, _ = client
    r = c.get("/admin/settings")
    assert r.status_code == 200
    assert "账号密钥" in r.text
    assert "发送目标" in r.text
    assert "名词说明" in r.text  # #11 名词解释


def test_schedule_only_validates_daily_send_time(client):
    """相隔五分钟的独立时间点有效，旧字段不再影响校验。"""
    c, factory = client
    for send_at in ("13:00", "13:05"):
        response = c.post("/admin/schedules", data={"name": send_at,
            "window_start": "invalid", "window_end": "invalid",
            "send_at": send_at, "send_day_offset": "invalid"}, follow_redirects=False)
        assert response.status_code == 303
    with factory() as s:
        assert s.query(Schedule).filter(Schedule.send_at.in_(["13:00", "13:05"])).count() == 2


def test_schedule_edit(client):
    """#7：定时可直接编辑。"""
    c, factory = client
    c.post("/admin/schedules", data={"name": "原始", "window_start": "08:00",
                                     "window_end": "24:00", "send_at": "01:00",
                                     "send_day_offset": 1})
    s = factory()
    sid = s.query(Schedule).filter_by(name="原始").first().id
    s.close()
    c.post(f"/admin/schedules/{sid}/edit", data={"name": "改后", "window_start": "09:00",
                                                "window_end": "23:00", "send_at": "02:00",
                                                "send_day_offset": 1})
    s = factory()
    r = s.get(Schedule, sid)
    assert r.name == "改后" and r.window_start == "08:00" and r.send_at == "02:00"
    s.close()


def test_rules_version_delete(client):
    """当前和被执行审计引用版本保留；未引用旧版本仍可删除。"""
    c, factory = client
    base_hash = re.search(r'name="base_hash" value="([a-f0-9]+)"', c.get('/admin/rules').text)[1]
    c.post("/admin/rules", data={"base_hash": base_hash, "yaml_text": open("rules/rules.yaml", encoding="utf-8").read(),
                                 "note": "v1"})
    s = factory()
    vid = s.query(RulesVersion).first().id
    s.close()
    response = c.post(f"/admin/rules/version/{vid}/delete", follow_redirects=False)
    assert response.status_code == 409
    s = factory()
    assert s.get(RulesVersion, vid) is not None
    old = RulesVersion(yaml_text='legacy: unreferenced', note='old unused version')
    s.add(old); s.commit(); old_id = old.id
    s.close()
    assert c.post(f'/admin/rules/version/{old_id}/delete', follow_redirects=False).status_code == 303
    with factory() as s: assert s.get(RulesVersion, old_id) is None


def test_batches_multi_delete(client):
    """#10：历史批次多选删除。"""
    c, factory = client
    s = factory()
    s.add(Batch(kind="manual", window_start=0, window_end=1, status="done"))
    s.add(Batch(kind="manual", window_start=0, window_end=2, status="done"))
    s.commit()
    ids = [b.id for b in s.query(Batch).all()]
    s.close()
    c.post("/admin/batches/delete", data={"batch_id": ids}, follow_redirects=False)
    s = factory()
    assert s.query(Batch).count() == 0
    s.close()
