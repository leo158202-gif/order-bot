"""下载 file/image 消息附件并转成文字，存入 messages.content。02 文档 4.2 节。

支持：xlsx/docx/doc/pdf 附件转写；图片（msgtype=image）经可配置的
OpenAI 兼容视觉接口抽文字后，轻量判断是否像订单信息。

铁律（C2 / #22）：
- 只处理 msgtype == "image"；msgtype == "emotion"（表情包/贴纸）永不下载、永不 OCR。
- 不自动清除待核；转写仅写入 content，供后续整理参考。
- 媒体文件落在当前工作区 data/media（测试 --test-workspace 下即隔离目录），不写仓库根机密。
"""
from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path
from typing import Callable, Optional

import docx
import httpx
import openpyxl
import pdfplumber

from app.models import Message
from app.wecom.sdk import WeComFinanceSdk

logger = logging.getLogger(__name__)

MEDIA_DIR = Path("data/media")
SUPPORTED_EXTS = {"xlsx", "docx", "doc", "pdf"}
MAX_TEXT_LEN = 20000

IMAGE_PLACEHOLDER = "[图片]"
IMAGE_TRANSCRIBED_PREFIX = "[图片转写]"
IMAGE_NON_ORDER_PREFIX = "[图片·非订单]"
_IMAGE_DONE_PREFIXES = (
    IMAGE_TRANSCRIBED_PREFIX,
    IMAGE_NON_ORDER_PREFIX,
)

_ORDER_HINT_RE = re.compile(
    r"(收货|地址|电话|手机|快递|物流|到付|自提|代收|"
    r"立柱|扶手|铝槽|地槽|盖子|螺丝|灯带|卡槽|角码|直接片|厚款|"
    r"\d{11}|\d{3,4}-\d{7,8}|"
    r"\d+\s*[支只米个套]|订|发货|补发|色样|料头)",
    re.I,
)


def is_image_msgtype(msgtype: str) -> bool:
    """企微真图。emotion（贴纸）必须走 False。"""
    return (msgtype or "") == "image"


def is_emotion_msgtype(msgtype: str) -> bool:
    return (msgtype or "") == "emotion"


def looks_like_order_text(text: str) -> bool:
    """零 token 启发式：是否像订单/收货相关文字。"""
    t = (text or "").strip()
    if len(t) < 2:
        return False
    return bool(_ORDER_HINT_RE.search(t))


def resolve_vision_cfg(cfg: Optional[dict]) -> Optional[dict]:
    """解析视觉 OCR 配置。

    文档默认（C2）：
    - 优先 config.vision：enabled（有 model 时默认开）、base_url、api_key、model。
    - 否则若 deepseek.vision_model 有值，复用 deepseek 的 base_url/api_key + 该模型。
    - 仅有 deepseek.model=deepseek-chat 不视为视觉能力（无图），返回 None。

    requirements 无 paddle/tesseract/easyocr；默认走多模态 chat completions。
    """
    if not cfg:
        return None
    vision = cfg.get("vision") or {}
    if vision.get("enabled") is False:
        return None
    model = (vision.get("model") or "").strip()
    base_url = (vision.get("base_url") or "").rstrip("/")
    api_key = vision.get("api_key") or ""
    ds = cfg.get("deepseek") or {}
    if model:
        return {
            "base_url": base_url or (ds.get("base_url") or "https://api.deepseek.com").rstrip("/"),
            "api_key": api_key or ds.get("api_key") or "",
            "model": model,
            "timeout": float(vision.get("timeout") or 60),
        }
    vmodel = (ds.get("vision_model") or "").strip()
    if vmodel:
        return {
            "base_url": (ds.get("base_url") or "https://api.deepseek.com").rstrip("/"),
            "api_key": ds.get("api_key") or "",
            "model": vmodel,
            "timeout": float(ds.get("vision_timeout") or 60),
        }
    return None


def download_media(sdk: WeComFinanceSdk, sdkfileid: str, msgid: str, ext: str) -> Path:
    """分片下载附件到 data/media/<msgid>.<ext>，返回路径。"""
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    out = MEDIA_DIR / f"{msgid.replace('/', '_')}.{ext}"
    if out.exists():
        return out
    content = sdk.get_media_data(sdkfileid)
    out.write_bytes(content)
    logger.info("附件已下载：%s（%d 字节）", out, len(content))
    return out


def file_to_text(path: Path) -> str:
    """把 xlsx/docx/pdf 转成纯文本。失败返回空串并记日志。"""
    ext = path.suffix.lower().lstrip(".")
    try:
        if ext == "xlsx":
            return _xlsx_to_text(path)
        if ext == "docx":
            return _docx_to_text(path)
        if ext == "doc":
            logger.warning(".doc 老格式暂不支持转文字（%s），请让客户发 xlsx/docx/pdf", path)
            return ""
        if ext == "pdf":
            return _pdf_to_text(path)
        logger.warning("不支持的附件类型：%s", ext)
        return ""
    except Exception as e:
        logger.error("附件转文字失败 %s: %s", path, e)
        return ""


def _xlsx_to_text(path: Path) -> str:
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    parts = []
    for ws in wb.worksheets:
        parts.append(f"[工作表 {ws.title}]")
        for row in ws.iter_rows(values_only=True):
            cells = [str(c) if c is not None else "" for c in row]
            line = "\t".join(cells).rstrip("\t")
            if line.strip():
                parts.append(line)
    wb.close()
    return "\n".join(parts)[:MAX_TEXT_LEN]


def _docx_to_text(path: Path) -> str:
    d = docx.Document(str(path))
    parts = [p.text for p in d.paragraphs if p.text.strip()]
    for table in d.tables:
        for row in table.rows:
            parts.append("\t".join(c.text for c in row.cells))
    return "\n".join(parts)[:MAX_TEXT_LEN]


def _pdf_to_text(path: Path) -> str:
    parts = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                parts.append(text)
    return "\n".join(parts)[:MAX_TEXT_LEN]


def process_pending_files(session, sdk: WeComFinanceSdk) -> int:
    """处理库里所有 file 类型但还没转文字的消息。返回处理条数。"""
    msgs = (
        session.query(Message)
        .filter(Message.msgtype == "file", Message.file_path != "")
        .all()
    )
    count = 0
    for m in msgs:
        raw = json.loads(m.raw_json)
        f = raw.get("file", {})
        ext = f.get("fileext", "").lower()
        sdkfileid = f.get("sdkfileid", "")
        if not sdkfileid:
            continue
        if not m.content.startswith("[文件]"):
            continue
        if ext not in SUPPORTED_EXTS:
            logger.info("跳过不支持的附件类型：%s (%s)", m.file_path, ext)
            continue
        try:
            path = download_media(sdk, sdkfileid, m.msgid, ext)
            text = file_to_text(path)
            if text:
                m.content = f"[文件 {m.file_path}]\n{text}"
                m.file_path = str(path)
                count += 1
                logger.info("附件转文字完成：%s -> %d 字符", m.file_path, len(text))
        except Exception as e:
            logger.error("处理附件失败 msgid=%s: %s", m.msgid, e)
    session.commit()
    return count


def _image_needs_ocr(content: str) -> bool:
    """仅对占位 [图片]（或空）做 OCR；已转写/非订单前缀跳过。"""
    c = (content or "").strip()
    if not c or c == IMAGE_PLACEHOLDER:
        return True
    if c.startswith(_IMAGE_DONE_PREFIXES):
        return False
    return False


def transcribe_image(
    path: Path,
    cfg: Optional[dict],
    *,
    http_post: Optional[Callable] = None,
) -> str:
    """用 OpenAI 兼容视觉接口从图片抽文字。无视觉配置时返回空串。"""
    vcfg = resolve_vision_cfg(cfg)
    if not vcfg:
        return ""
    if not vcfg.get("api_key"):
        logger.warning("视觉 OCR 已配置 model 但缺少 api_key，跳过转写")
        return ""
    raw = path.read_bytes()
    if not raw:
        return ""
    mime = "image/jpeg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        mime = "image/png"
    elif raw[:6] in (b"GIF87a", b"GIF89a"):
        mime = "image/gif"
    elif raw[:2] == b"BM":
        mime = "image/bmp"
    elif len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        mime = "image/webp"
    b64 = base64.b64encode(raw).decode("ascii")
    data_url = f"data:{mime};base64,{b64}"
    url = f"{vcfg['base_url']}/chat/completions"
    payload = {
        "model": vcfg["model"],
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "请提取这张图片中的全部可见文字，按原顺序输出纯文本。"
                            "不要解释、不要猜测未写出的订单字段。"
                            "若几乎没有文字，只输出空。"
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
    }
    headers = {"Authorization": f"Bearer {vcfg['api_key']}"}
    post = http_post or httpx.post
    try:
        resp = post(url, headers=headers, json=payload, timeout=vcfg["timeout"])
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error("视觉 OCR 请求失败：%s", e)
        return ""
    try:
        text = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        logger.error("视觉 OCR 响应无法解析：%s", str(data)[:200])
        return ""
    return str(text).strip()[:MAX_TEXT_LEN]


def process_pending_images(
    session,
    sdk: WeComFinanceSdk,
    cfg: Optional[dict] = None,
    *,
    transcribe_fn: Optional[Callable] = None,
    download: bool = True,
) -> dict:
    """处理待转写的 image 消息。

    返回统计 dict。查询仅取 msgtype==image；raw.msgtype==emotion 双保险不下载不 OCR。
    """
    stats = {
        "scanned": 0,
        "skipped_emotion": 0,
        "skipped_no_vision": 0,
        "skipped_done": 0,
        "transcribed": 0,
        "order_like": 0,
        "non_order": 0,
        "errors": 0,
    }

    msgs = session.query(Message).filter(Message.msgtype == "image").all()
    vcfg = resolve_vision_cfg(cfg)
    ocr = transcribe_fn or (lambda path, c: transcribe_image(path, c))

    for m in msgs:
        stats["scanned"] += 1
        if not is_image_msgtype(m.msgtype):
            continue
        if not _image_needs_ocr(m.content):
            stats["skipped_done"] += 1
            continue

        raw = json.loads(m.raw_json or "{}")
        raw_mt = raw.get("msgtype") or m.msgtype
        if is_emotion_msgtype(raw_mt):
            stats["skipped_emotion"] += 1
            logger.info(
                "跳过 msgid=%s：raw.msgtype=emotion，不下载不OCR（省 token）",
                m.msgid,
            )
            continue

        img = raw.get("image") or {}
        sdkfileid = img.get("sdkfileid") or ""
        if not sdkfileid:
            logger.warning("image 消息无 sdkfileid，跳过 msgid=%s", m.msgid)
            stats["errors"] += 1
            continue

        if transcribe_fn is None and not vcfg:
            stats["skipped_no_vision"] += 1
            logger.info(
                "跳过图片 OCR msgid=%s：未配置 vision.model / deepseek.vision_model"
                "（deepseek-chat 无视觉；见 docs/验证记录/C2）",
                m.msgid,
            )
            continue

        try:
            if download:
                path = download_media(sdk, sdkfileid, m.msgid, "jpg")
                m.file_path = str(path)
            else:
                path = Path(m.file_path) if m.file_path else MEDIA_DIR / f"{m.msgid}.jpg"

            text = ocr(path, cfg)
            if not (text or "").strip():
                logger.info("图片转写空结果 msgid=%s，保留占位", m.msgid)
                continue

            if looks_like_order_text(text):
                m.content = f"{IMAGE_TRANSCRIBED_PREFIX}\n{text.strip()}"
                stats["order_like"] += 1
                logger.info("图片转写·像订单 msgid=%s -> %d 字符", m.msgid, len(text))
            else:
                m.content = f"{IMAGE_NON_ORDER_PREFIX}\n{text.strip()}"
                stats["non_order"] += 1
                logger.info("图片转写·非订单，不追加贵后续 msgid=%s", m.msgid)
            stats["transcribed"] += 1
        except Exception as e:
            stats["errors"] += 1
            logger.error("处理图片失败 msgid=%s: %s", m.msgid, e)

    session.commit()
    return stats


def count_emotion_messages(session) -> int:
    """供 dry-run / 日志：emotion 条数（本模块永不 OCR 它们）。"""
    return session.query(Message).filter(Message.msgtype == "emotion").count()


def skip_emotion_reason() -> str:
    return "msgtype=emotion（表情包/贴纸），跳过下载与OCR以省 token"
