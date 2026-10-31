"""企业微信会话存档探测：从指定 seq 拉取消息、解密、打印报告。

用法：
  .venv\\Scripts\\python.exe scripts\\wecom_probe.py            # 从上次位置继续
  .venv\\Scripts\\python.exe scripts\\wecom_probe.py --seq 0    # 从头拉
  .venv\\Scripts\\python.exe scripts\\wecom_probe.py --limit 200

报告打印到控制台，同时追加写入 docs/阶段0-probe记录/（消息内容逐条列出）。
电话号码在落盘报告里中间 4 位打码。
"""
import argparse
import base64
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

from app.wecom.sdk import WeComFinanceSdk, WeComSdkError

PROBE_DIR = Path("docs/阶段0-probe记录")
STATE_FILE = Path("data/probe_state.json")
PHONE_RE = re.compile(r"(1\d{2})\d{4}(\d{4})")


def mask_phone(text: str) -> str:
    return PHONE_RE.sub(r"\1****\2", text)


def load_private_key(path: str):
    p = Path(path)
    if not p.is_absolute():
        p = Path("data") / p  # 配置里的路径相对 data/ 目录
    return serialization.load_pem_private_key(p.read_bytes(), password=None)


def rsa_decrypt(priv, b64_cipher: str) -> bytes:
    return priv.decrypt(base64.b64decode(b64_cipher), padding.PKCS1v15())


def get_access_token(corpid: str, secret: str) -> str:
    resp = httpx.get(
        "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
        params={"corpid": corpid, "corpsecret": secret}, timeout=10,
    ).json()
    if resp.get("errcode") != 0:
        raise RuntimeError(f"gettoken 失败: {resp}")
    return resp["access_token"]


def check_room_agree(token: str, roomid: str) -> dict:
    resp = httpx.post(
        "https://qyapi.weixin.qq.com/cgi-bin/msgaudit/check_room_agree",
        params={"access_token": token},
        json={"roomid": roomid}, timeout=10,
    ).json()
    return resp


def summarize_msg(msg: dict) -> str:
    mt = msg.get("msgtype", "?")
    action = msg.get("action", "?")
    parts = [f"[{mt}/{action}]"]
    if mt == "text":
        parts.append(msg.get("text", {}).get("content", ""))
    elif mt == "file":
        f = msg.get("file", {})
        parts.append(f"文件 {f.get('filename')} ({f.get('filesize')}B, sdkfileid 末8位 …{f.get('sdkfileid', '')[-8:]})")
    elif mt == "revoke":
        parts.append(f"撤回 {msg.get('revoke', {}).get('pre_msgid')}")
    elif mt in ("agree", "disagree"):
        parts.append(f"{mt}: {msg.get(mt, {}).get('userid')}")
    elif mt == "image":
        parts.append("(图片)")
    elif mt == "voice":
        parts.append("(语音)")
    else:
        parts.append(json.dumps(msg.get(mt, {}), ensure_ascii=False)[:200])
    return " ".join(parts)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seq", type=int, default=None, help="起始 seq（默认读 data/probe_state.json，没有则 0）")
    ap.add_argument("--limit", type=int, default=1000)
    args = ap.parse_args()

    cfg = yaml.safe_load(Path("data/config.yaml").read_text(encoding="utf-8"))
    corpid = cfg["wecom"]["corpid"]
    secret = cfg["wecom"]["archive_secret"]
    key_map = {int(k): v for k, v in cfg["wecom"]["private_keys"].items()}
    # 配置里的键是字符串数字，统一转成 int 后按 ver 直接索引

    seq = args.seq
    if seq is None and STATE_FILE.exists():
        seq = json.loads(STATE_FILE.read_text())["last_seq"]
    if seq is None:
        seq = 0

    lines = []  # 报告行

    def out(s: str = ""):
        print(s)
        lines.append(mask_phone(s))

    out(f"# probe 时间 {datetime.now():%Y-%m-%d %H:%M:%S}，起始 seq={seq}")

    priv_keys = {}
    try:
        sdk = WeComFinanceSdk(corpid, secret)
    except WeComSdkError as e:
        out(f"SDK 初始化失败：{e}（10009=IP 非法，请核对可信 IP；其他见头文件错误码）")
        sys.exit(2)

    messages = []
    with sdk:
        while True:
            try:
                raw = sdk.get_chat_data(seq, args.limit)
            except WeComSdkError as e:
                out(f"GetChatData 失败：{e}")
                sys.exit(2)
            data = json.loads(raw)
            if data.get("errcode") != 0:
                out(f"返回错误：{data.get('errcode')} {data.get('errmsg')}")
                sys.exit(2)
            chatdata = data.get("chatdata", [])
            out(f"本页拉到 {len(chatdata)} 条（seq 起点 {seq}）")
            for item in chatdata:
                seq = item["seq"]
                ver = item.get("publickey_ver")
                try:
                    if ver not in priv_keys:
                        priv_keys[ver] = load_private_key(key_map[ver])
                    key = rsa_decrypt(priv_keys[ver], item["encrypt_random_key"])
                    msg = json.loads(sdk.decrypt_data(key, item["encrypt_chat_msg"]))
                    messages.append((item["seq"], msg))
                except Exception as e:
                    out(f"  seq={item['seq']} 解密失败（publickey_ver={ver}）：{e}")
            if len(chatdata) < args.limit:
                break

    out(f"\n共 {len(messages)} 条消息，最大 seq={seq}")
    roomids = {}
    for s, msg in messages:
        roomid = msg.get("roomid") or "(单聊)"
        roomids.setdefault(roomid, []).append((s, msg))

    for roomid, msgs in roomids.items():
        out(f"\n## 会话 {roomid}（{len(msgs)} 条）")
        for s, msg in msgs:
            ts = datetime.fromtimestamp(msg.get("msgtime", 0) / 1000).strftime("%m-%d %H:%M:%S")
            out(f"  seq={s} {ts} from={msg.get('from')} {summarize_msg(msg)}")

    # check_room_agree 对群聊
    group_rooms = [r for r in roomids if r != "(单聊)"]
    if group_rooms:
        out("\n## check_room_agree")
        token = get_access_token(corpid, secret)
        for roomid in group_rooms:
            r = check_room_agree(token, roomid)
            out(f"  {roomid}: {json.dumps(r, ensure_ascii=False)}")

    STATE_FILE.write_text(json.dumps({"last_seq": seq}))
    PROBE_DIR.mkdir(parents=True, exist_ok=True)
    report = PROBE_DIR / f"probe-{datetime.now():%Y%m%d-%H%M%S}.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n报告已保存：{report}；当前 seq 已记录到 {STATE_FILE}")


if __name__ == "__main__":
    main()
