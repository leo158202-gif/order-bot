"""FastAPI 应用：主界面 + 管理后台。只监听 127.0.0.1。"""
import secrets
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from app.maintenance import MaintenanceMiddleware

from app.web.admin_routes import router as admin_router
from app.web.routes import router
from app.web.log_routes import router as logs_router
from app.web.diagnostician_routes import router as doctor_router

WEB_DIR = Path(__file__).resolve().parent  # app/web


def _session_secret() -> str:
    f = Path(os.environ['ORDER_BOT_DATA_DIR']) / 'session_secret' if os.environ.get('ORDER_BOT_DATA_DIR') else WEB_DIR.parent.parent / "data" / "session_secret"
    if f.exists():
        return f.read_text().strip()
    f.parent.mkdir(parents=True, exist_ok=True)
    s = secrets.token_hex(32)
    f.write_text(s)
    return s


def create_app() -> FastAPI:
    app = FastAPI(title="order-bot")
    app.add_middleware(SessionMiddleware, secret_key=_session_secret())
    app.add_middleware(MaintenanceMiddleware)
    app.include_router(router)
    app.include_router(admin_router)
    app.include_router(logs_router)
    app.include_router(doctor_router)
    @app.on_event("startup")
    def start_diagnostician():
        if getattr(app.state, "diagnostician_service", None) is None:
            from app.diagnostician.integration import build_service
            app.state.diagnostician_service = build_service(Path("data"))
    @app.on_event("shutdown")
    def stop_diagnostician():
        service = getattr(app.state, "diagnostician_service", None)
        if service is not None:
            service.close()
    static_dir = WEB_DIR / "static"
    static_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    return app


app = create_app()
