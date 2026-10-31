"""飞书联通探测：建表格 → 写表头和示例 → 合并 A 列 → 表头样式 → 发群消息。

用法：.venv\\Scripts\\python.exe scripts\\feishu_probe.py
在测试文件夹里创建「测试表格-YYYYMMDD-HHMM」，写入 04 文档示例 2，发链接到测试群。
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import yaml

BASE = "https://open.feishu.cn/open-apis"

# 04 文档示例 2：H 款立柱（8 行数据）
HEADER = ["序号", "产品", "配置", "数量", "收货人", "电话", "地址", "物流"]
ROWS = [
    [1, "灰色内卡槽立柱 焊接 H款", "【3.0】1170高 单槽", "2支", "收货人B【急！", "138****0002-5672", "某省某市某地址；货拉拉", ""],
    ["", "", "【3.0】1170高 双槽", "1支", "", "", "", ""],
    ["", "焊接 新款三槽主体 卡条离地10公分 H头用锁螺丝的", "", "", "", "", "", ""],
    ["", "灰色立柱盖子", "", "3个", "", "", "", ""],
    ["", "膨胀螺丝", "", "12个", "", "", "", ""],
    ["", "小号钻尾螺丝", "", "16个", "", "", "", ""],
    ["", "50*80灰色铝合金扶手", "1支2米", "2米", "", "", "", ""],
    ["", "封口", "灰色50*80配件", "2个", "", "", "", ""],
]


class Feishu:
    def __init__(self, app_id: str, app_secret: str):
        self.app_id, self.app_secret = app_id, app_secret
        self._token = None
        self._token_expire = 0

    @property
    def token(self) -> str:
        if not self._token or time.time() > self._token_expire:
            r = httpx.post(f"{BASE}/auth/v3/tenant_access_token/internal",
                           json={"app_id": self.app_id, "app_secret": self.app_secret},
                           timeout=10).json()
            if r.get("code") != 0:
                raise RuntimeError(f"获取 tenant_access_token 失败: {r}")
            self._token = r["tenant_access_token"]
            self._token_expire = time.time() + r.get("expire", 7200) - 300
        return self._token

    def req(self, method: str, path: str, **kw) -> dict:
        from app.test_runtime import check_request
        check_request(method, path, kw)
        r = httpx.request(method, f"{BASE}{path}",
                          headers={"Authorization": f"Bearer {self.token}"},
                          timeout=20, **kw).json()
        if r.get("code") != 0:
            raise RuntimeError(f"{method} {path} 失败: code={r.get('code')} msg={r.get('msg')}")
        return r.get("data", {})


def main() -> None:
    raise RuntimeError('旧建表探测已停用；请使用指定测试表和隔离测试入口，不创建其它表')
    cfg = yaml.safe_load(Path("data/config.yaml").read_text(encoding="utf-8"))
    fs = Feishu(cfg["feishu"]["app_id"], cfg["feishu"]["app_secret"])
    folder_token = cfg["feishu"]["test"]["folder_token"]
    chat_id = cfg["feishu"]["test"]["chat_id"]

    print("1. 获取 tenant_access_token ... OK（末4位 …%s）" % fs.token[-4:])

    # 2. 在测试文件夹建表格
    title = "测试表格-" + datetime.now().strftime("%Y%m%d-%H%M")
    data = fs.req("POST", "/sheets/v3/spreadsheets",
                  json={"title": title, "folder_token": folder_token})
    ss_token = data["spreadsheet"]["spreadsheet_token"]
    ss_url = data["spreadsheet"]["url"]
    print(f"2. 已创建表格「{title}」: {ss_url}")

    # 3. 查默认工作表
    data = fs.req("GET", f"/sheets/v3/spreadsheets/{ss_token}/sheets/query")
    sheet = data["sheets"][0]
    sheet_id = sheet["sheet_id"]
    print(f"3. 默认工作表 sheet_id={sheet_id}，改名为「10.20」")
    fs.req("POST", f"/sheets/v2/spreadsheets/{ss_token}/sheets_batch_update",
           json={"requests": [{"updateSheet": {"properties": {"sheetId": sheet_id, "title": "10.20"}}}]})

    # 4. 写表头 + 示例数据
    values = [HEADER] + ROWS
    n_rows = len(values)
    fs.req("PUT", f"/sheets/v2/spreadsheets/{ss_token}/values",
           json={"valueRange": {"range": f"{sheet_id}!A1:H{n_rows}", "values": values}})
    print(f"4. 已写入表头 + {len(ROWS)} 行示例数据（A1:H{n_rows}）")

    # 5. 合并 A 列（序号列，A2:A9）
    fs.req("POST", f"/sheets/v2/spreadsheets/{ss_token}/merge_cells",
           json={"range": f"{sheet_id}!A2:A{n_rows}", "mergeType": "MERGE_ALL"})
    # 焊接描述行（第 4 行）：B4:D4 横向合并
    fs.req("POST", f"/sheets/v2/spreadsheets/{ss_token}/merge_cells",
           json={"range": f"{sheet_id}!B4:D4", "mergeType": "MERGE_ALL"})
    print("5. 已合并 A2:A9（序号）和 B4:D4（焊接描述行）")

    # 6. 表头样式：蓝底白字粗体居中；数据居中
    fs.req("PUT", f"/sheets/v2/spreadsheets/{ss_token}/styles_batch_update",
           json={"data": [
               {"ranges": [f"{sheet_id}!A1:H1"],
                "style": {"backColor": "#4472C4", "foreColor": "#FFFFFF",
                          "bold": True, "hAlign": 1, "vAlign": 1, "wordWrap": True}},
               {"ranges": [f"{sheet_id}!A2:H{n_rows}"],
                "style": {"hAlign": 1, "vAlign": 1}},
           ]})
    print("6. 已设置表头样式（蓝底白字粗体居中）和数据居中")

    # 7. 列宽（B≈270px, C≈218px, E≈158px, G≈525px, H≈100px）
    for col, px in [("B", 270), ("C", 218), ("E", 158), ("G", 525), ("H", 100)]:
        fs.req("PUT", f"/sheets/v2/spreadsheets/{ss_token}/dimension_range",
               json={"dimension": {"sheetId": sheet_id, "majorDimension": "COLUMNS",
                                   "startIndex": ord(col) - 65, "endIndex": ord(col) - 64},
                     "dimensionProperties": {"visible": True, "fixedSize": px}})
    print("7. 已设置列宽")

    # 8. 列出机器人所在的群，确认测试群在其中
    data = fs.req("GET", "/im/v1/chats", params={"page_size": 50})
    chats = data.get("items", [])
    names = {c["chat_id"]: c.get("name", "") for c in chats}
    print(f"8. 机器人所在群（{len(chats)} 个）：")
    for cid, name in names.items():
        mark = " <-- 测试群" if cid == chat_id else ""
        print(f"   {cid}  {name}{mark}")
    if chat_id not in names:
        print("   警告：配置的测试群 chat_id 不在列表里，请确认机器人已入群")

    # 9. 发群消息
    text = (f"【联通测试】飞书表格写入成功\n"
            f"表格：{ss_url}（工作表 10.20，第 1–{n_rows} 行）\n"
            f"记录时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
    fs.req("POST", "/im/v1/messages", params={"receive_id_type": "chat_id"},
           json={"receive_id": chat_id, "msg_type": "text",
                 "content": json.dumps({"text": text}, ensure_ascii=False)})
    print("9. 已发群消息到测试群")

    print(f"\n完成。请打开表格检查格式：{ss_url}")


if __name__ == "__main__":
    main()
