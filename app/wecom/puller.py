"""增量拉取会话存档消息、解密、落库。流程见 02 文档 4.2 节。

- seq 与消息在同一事务里提交：中途异常时整体回滚，seq 不前进
- msgid 唯一：重复拉取不产生重复消息
- 撤回消息（revoke）：标记原消息 revoked；已整理过的由批次提示核对
- 只处理有 roomid 的群聊消息；未启用的群只更新 rooms 表和样例
"""
from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from sqlalchemy.orm import Session

from app.models import Message, Room, State
from app.wecom.sdk import WeComFinanceSdk, WeComSdkError

logger = logging.getLogger(__name__)

MAX_SAMPLE = 5  # 群管理页展示的最近样例条数


def load_private_key(path: str):
    p = Path(path)
    if not p.is_absolute():
        p = Path("data") / p  # 配置里的路径相对 data/
    return serialization.load_pem_private_key(p.read_bytes(), password=None)


def rsa_decrypt(priv, b64_cipher: str) -> bytes:
    return priv.decrypt(base64.b64decode(b64_cipher), padding.PKCS1v15())


def _get_state(session: Session, key: str, default: str = "0") -> str:
    row = session.get(State, key)
    return row.value if row else default


def _set_state(session: Session, key: str, value: str) -> None:
    row = session.get(State, key)
    if row:
        row.value = value
    else:
        session.add(State(key=key, value=value))


def _extract_content(msg: dict) -> str:
    mt = msg.get("msgtype", "")
    if mt == "text":
        return msg.get("text", {}).get("content", "")
    if mt == "revoke":
        return ""
    if mt == "file":
        f = msg.get("file", {})
        return f"[文件] {f.get('filename', '')}"
    if mt == "image":
        return "[图片]"
    if mt == "emotion":
        # 企微贴纸/表情：C2 永不 OCR/下载；占位便于日志区分
        return "[emotion]"
    if mt == "voice":
        return "[语音]"
    if mt == "video":
        return "[视频]"
    return f"[{mt}]"


def _update_room(session: Session, msg: dict) -> None:
    roomid = msg.get("roomid", "")
    if not roomid:
        return
    room = session.get(Room, roomid)
    if room is None:
        room = Room(roomid=roomid)
        session.add(room)
    room.last_msg_at = datetime.fromtimestamp(msg.get("msgtime", 0) / 1000, tz=timezone.utc)
    samples = json.loads(room.sample_json or "[]")
    samples.append({
        "time": msg.get("msgtime", 0),
        "sender": msg.get("from", ""),
        "content": _extract_content(msg)[:100],
    })
    room.sample_json = json.dumps(samples[-MAX_SAMPLE:], ensure_ascii=False)


def pull_once(session: Session, sdk: WeComFinanceSdk, priv_keys: dict,
              limit: int = 1000) -> int:
    """从上次 seq 拉一轮（可能多页），返回本次入库的消息条数。

    每一页：解密 → upsert → 更新 seq，同一事务提交。
    中途异常时该页回滚，seq 停在上一页末尾，下次重拉不重不漏。
    """
    seq = int(_get_state(session, "last_seq", "0"))
    total = 0
    while True:
        raw = sdk.get_chat_data(seq, limit)
        data = json.loads(raw)
        if data.get("errcode") != 0:
            # 带 errcode 抛 WeComSdkError，让上层拿到真实错误码（301042 白名单等，#31）
            raise WeComSdkError(data.get("errcode"),
                                f"GetChatData: {data.get('errmsg')}")
        chatdata = data.get("chatdata", [])
        if not chatdata:
            break

        try:
            page_max_seq = seq
            for item in chatdata:
                item_seq = item["seq"]
                ver = item.get("publickey_ver")
                if ver not in priv_keys:
                    raise RuntimeError(f"没有 publickey_ver={ver} 对应的私钥")
                key = rsa_decrypt(priv_keys[ver], item["encrypt_random_key"])
                msg = json.loads(sdk.decrypt_data(key, item["encrypt_chat_msg"]))
                _save_message(session, item_seq, msg)
                page_max_seq = item_seq
            _set_state(session, "last_seq", str(page_max_seq))
            session.commit()  # 消息和 seq 同事务
            total += len(chatdata)
            seq = page_max_seq
        except Exception:
            session.rollback()
            raise

        if len(chatdata) < limit:
            break
    return total


def _save_message(session: Session, seq: int, msg: dict) -> None:
    """落库一条消息（含撤回处理、rooms 样例更新）。msgid 唯一，重复忽略。"""
    msgid = msg.get("msgid", "")
    if not msgid:
        return
    if session.get(Message, msgid):  # 幂等：重复拉取不产生重复消息
        return

    msgtype = msg.get("msgtype", "")
    file_path = ""
    if msgtype == "file":
        file_path = msg.get("file", {}).get("filename", "")  # 实际下载在 media.py，这里先记文件名

    session.add(Message(
        msgid=msgid, seq=seq,
        roomid=msg.get("roomid", ""),
        sender=msg.get("from", ""),
        msgtime=msg.get("msgtime", 0),
        msgtype=msgtype,
        content=_extract_content(msg),
        raw_json=json.dumps(msg, ensure_ascii=False),
        file_path=file_path,
    ))
    _update_room(session, msg)

    # 撤回：标记原消息
    if msgtype == "revoke":
        pre_msgid = msg.get("revoke", {}).get("pre_msgid", "")
        if pre_msgid:
            orig = session.get(Message, pre_msgid)
            if orig:
                orig.revoked = True
                logger.info("消息 %s 被撤回（revoke by %s）", pre_msgid, msgid)


def build_priv_keys(private_keys_cfg: dict) -> dict:
    """config 的 private_keys {"1": "keys/private.pem"} -> {1: 私钥对象}。"""
    return {int(ver): load_private_key(path) for ver, path in private_keys_cfg.items()}
