"""管理后台公共：密码校验、导航布局。

密码可留空不设；设了之后 /admin/* 需要登录（session cookie）。
"""
import functools

import bcrypt
import yaml
from fastapi import Request
from fastapi.responses import RedirectResponse
from pathlib import Path

ADMIN_PAGES = [
    ("/admin/rooms", "群管理"),
    ("/admin/schedules", "定时设置"),
    ("/admin/rules", "简称规则"),
    ("/admin/pending/gold", "金标准对照"),
    ("/admin/batches", "历史批次"), ("/admin/history", "历史恢复/重发"),
    ("/admin/status", "运行状态"),
    ("/admin/logs", "运行日志"),
    ("/admin/ip", "IP 白名单"),
    ("/admin/settings", "系统设置"),
]


def _cfg() -> dict:
    return yaml.safe_load(Path("data/config.yaml").read_text(encoding="utf-8"))


def password_hash() -> str:
    return _cfg().get("admin_password_hash", "") or ""


def check_password(pw: str) -> bool:
    h = password_hash()
    if not h:
        return True
    return bcrypt.checkpw(pw.encode(), h.encode())


def is_authed(request: Request) -> bool:
    if not password_hash():
        return True
    return request.session.get("admin") is True


def require_auth(func):
    @functools.wraps(func)
    def wrapper(request: Request, *a, **kw):
        if not is_authed(request):
            return RedirectResponse("/admin/login", status_code=303)
        return func(request, *a, **kw)
    return wrapper


def layout(content: str, active: str = "") -> str:
    nav = "".join(
        f'<a href="{url}" class="{"active" if url == active else ""}">{name}</a>'
        for url, name in ADMIN_PAGES)
    return NAV_HTML.replace("{{nav}}", nav).replace("{{content}}", content)


NAV_HTML = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>管理后台 - order-bot</title>
<style>
  body { font-family: "Microsoft YaHei", sans-serif; margin: 0; background: #f5f6f7; }
  .topbar { background: #2b3a4d; color: #fff; padding: 0 20px; display: flex; align-items: center; }
  .topbar .brand { font-weight: bold; padding: 14px 20px 14px 0; }
  .topbar a { color: #cfd8e3; text-decoration: none; padding: 14px 14px; font-size: 14px; }
  .topbar a.active { color: #fff; border-bottom: 2px solid #3370ff; }
  .topbar .home { margin-left: auto; }
  .main { max-width: 1100px; margin: 20px auto; padding: 0 20px; }
  .card { background: #fff; border-radius: 8px; padding: 18px; margin-bottom: 16px;
          box-shadow: 0 1px 3px rgba(0,0,0,.06); }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { padding: 8px 10px; border-bottom: 1px solid #eee; text-align: left; }
  th { color: #999; font-weight: normal; font-size: 12px; }
  input[type=text], input[type=password], select, textarea {
    padding: 6px 10px; border: 1px solid #ddd; border-radius: 4px; font-size: 14px; }
  button { padding: 6px 16px; border: none; border-radius: 4px; cursor: pointer;
           background: #3370ff; color: #fff; }
  button.ghost { background: #f0f0f0; color: #333; }
  button.danger { background: #f8d7da; color: #721c24; }
  .ok { color: #1a7f37; } .bad { color: #d32f2f; }
  .muted { color: #999; font-size: 12px; }
  .reason { background: #fff3cd; color: #856404; padding: 2px 8px; border-radius: 4px;
            font-size: 12px; }
</style></head><body>
<div class="topbar">
  <span class="brand">order-bot 管理后台</span>
  {{nav}}
  <a class="home" href="/">← 主界面</a>
</div>
<div class="main">{{content}}</div>
</body></html>"""
