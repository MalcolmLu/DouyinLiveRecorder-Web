"""Authenticated HTML console and a small same-origin JSON API."""
import json
import logging
import os
from pathlib import Path
import secrets
import time
from urllib.parse import urlsplit
import uuid

from flask import Flask, g, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException
from auth import AuthRepository, AuthService
from settings_service import ConfigService
from repository import ConfigRepository, QueueRepository
from services import DashboardService
from shared import AppError, Paths
from platform_utils import source_root

LOG = logging.getLogger("recorder_web")


def create_app(paths=None, testing=False, desktop=None):
    paths = paths or Paths.from_env()
    app = Flask(__name__, static_folder=None)
    app.config.update(TESTING=testing, MAX_CONTENT_LENGTH=32768)
    secure = not testing and desktop is None and os.environ.get("GUI_ENV") != "development"
    auth_repo = AuthRepository(paths.state)
    auth = AuthService(auth_repo)
    config = ConfigService(ConfigRepository(paths))
    queue = QueueRepository(paths)
    dashboard = DashboardService(paths, config, queue)
    app.extensions["recorder_web"] = {"auth": auth, "auth_repo": auth_repo, "config": config,
                                     "queue": queue, "dashboard": dashboard, "paths": paths}
    static = source_root() / "gui/static"
    allowed_hosts = set(filter(None, os.environ.get("GUI_ALLOWED_HOSTS", "").split(",")))

    def body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise AppError("请求必须是 JSON 对象。")
        return data

    @app.before_request
    def protect():
        g.request_id = uuid.uuid4().hex[:16]
        if desktop is not None and not testing:
            if request.remote_addr!="127.0.0.1" or request.host!=app.config.get("DESKTOP_ALLOWED_HOST"):
                raise AppError("桌面服务仅允许本机应用访问。","local_only",403)
        if allowed_hosts and request.host not in allowed_hosts and not testing:
            raise AppError("访问主机不在允许列表中。", "invalid_host", 400)
        if secure and not request.is_secure:
            raise AppError("请通过 HTTPS 访问。", "https_required", 403)
        origin = request.headers.get("Origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin and origin != request.host_url.rstrip("/"):
            raise AppError("请求来源不正确，请从管理页面操作。", "origin", 403)
        g.session = auth_repo.session(request.cookies.get("recorder_session"))
        public = ("/", "/health", "/ready", "/api/session", "/api/login")
        if request.path.startswith("/api/") and request.path not in public:
            if not g.session:
                raise AppError("请先登录网页管理界面。", "unauthorized", 401)
            if request.method not in ("GET", "HEAD"):
                if not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), g.session["csrf"]):
                    raise AppError("页面验证已过期，请刷新后重试。", "csrf", 403)

    @app.after_request
    def headers(response):
        response.headers.update({
            "X-Request-ID": getattr(g, "request_id", ""),
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            "Cache-Control": "no-store",
        })
        LOG.info(json.dumps({"event": "http", "request_id": getattr(g, "request_id", None),
                             "method": request.method, "path": request.path, "status": response.status_code}))
        return response

    @app.errorhandler(Exception)
    def error(exc):
        if isinstance(exc, AppError):
            return jsonify(error={"code": exc.code, "message": exc.message},
                           request_id=getattr(g, "request_id", None)), exc.status
        if isinstance(exc, HTTPException):
            return jsonify(error={"code": "http_error", "message": "请求无法完成，请检查后重试。"}), exc.code
        LOG.exception(json.dumps({"event": "unexpected_error", "request_id": getattr(g, "request_id", None)}))
        return jsonify(error={"code": "internal", "message": "暂时无法完成操作，请稍后重试。"},
                       request_id=getattr(g, "request_id", None)), 500

    @app.get("/")
    def index():
        return send_from_directory(static, "index.html")

    @app.get("/static/<path:name>")
    def assets(name):
        return send_from_directory(static, name)

    @app.get("/health")
    def health():
        return jsonify(ok=True)

    @app.get("/ready")
    def ready():
        ready = config.ini.exists() and config.urls.exists()
        return jsonify(ready=ready), 200 if ready else 503

    @app.get("/api/session")
    def session_info():
        return jsonify(authenticated=bool(g.session), csrf=g.session["csrf"] if g.session else None,
                       setup_required=desktop is None and not bool(auth_repo.password_hash()),
                       desktop=desktop is not None,
                       environment="development" if os.environ.get("GUI_ENV") == "development" else "production")

    @app.post("/api/login")
    def login():
        token, csrf = auth.login(body().get("password"), request.remote_addr or "unknown")
        response = jsonify(authenticated=True, csrf=csrf)
        response.set_cookie("recorder_session", token, httponly=True, secure=secure,
                            samesite="Strict", max_age=86400, path="/")
        return response

    @app.post("/api/logout")
    def logout():
        auth_repo.delete_session(g.session["hash"])
        response = jsonify(ok=True)
        response.delete_cookie("recorder_session", secure=secure, httponly=True, samesite="Strict")
        return response

    @app.get("/api/dashboard")
    def live_dashboard():
        snapshot=dashboard.snapshot()
        snapshot["encoding"]={"encoder":config.settings()["encoder"]}
        if desktop is not None:snapshot["desktop"]=desktop.status()
        return jsonify(snapshot)

    if desktop is not None:
        @app.get("/api/desktop")
        def desktop_info():
            return jsonify(desktop.snapshot())

        @app.put("/api/desktop")
        def desktop_save():
            return jsonify(desktop.save(body()))

        @app.post("/api/desktop/start")
        def desktop_start():
            desktop.start()
            return jsonify(ok=True,message="采集器已启动。")

        @app.post("/api/desktop/stop")
        def desktop_stop():
            desktop.request_stop()
            return jsonify(ok=True,message="正在停止采集器并保存录像；转码队列继续处理。"),202

        @app.post("/api/desktop/test-encoder")
        def desktop_encoder():
            return jsonify(desktop.test_encoder(body().get("encoder")))

    @app.get("/api/jobs")
    def jobs():
        status = request.args.get("status", "all")
        search = request.args.get("search", "").strip()
        try:
            page = int(request.args.get("page", "1"))
        except ValueError:
            raise AppError("页码不正确。")
        if status not in ("all", "queued", "running", "published", "done", "failed") or len(search) > 100 or not 1 <= page <= 10000:
            raise AppError("筛选条件不正确。")
        return jsonify(queue.jobs(status, search, page))

    @app.post("/api/jobs/<identifier>/retry")
    def retry(identifier):
        queue.retry(identifier)
        return jsonify(ok=True, message="已重新排队，采集器会自动接续处理。")

    @app.delete("/api/jobs/<identifier>")
    def delete_job_record(identifier):
        queue.delete_record(identifier)
        return jsonify(ok=True, message="转码记录已删除，录像文件已保留。")

    @app.post("/api/rooms")
    def create_room():
        identifier = config.create_room(body())
        return jsonify(ok=True, id=identifier, revision=config.revision()), 201

    @app.patch("/api/rooms/<identifier>")
    def update_room(identifier):
        updated = config.update_room(identifier, body())
        return jsonify(ok=True, id=updated, revision=config.revision())

    @app.delete("/api/rooms/<identifier>")
    def delete_room(identifier):
        config.delete_room(identifier, body())
        return jsonify(ok=True, revision=config.revision())

    @app.get("/api/deleted-rooms")
    def deleted_rooms():
        with config.lock:
            return jsonify(items=config.deleted_rooms(), revision=config.revision())

    @app.post("/api/deleted-rooms/<identifier>/restore")
    def restore_room(identifier):
        updated = config.restore_room(identifier, body())
        return jsonify(ok=True, id=updated, revision=config.revision())

    @app.get("/api/settings")
    def settings():
        with config.lock:
            return jsonify(config.settings())

    @app.put("/api/settings")
    def save_settings():
        config.save_settings(body())
        return jsonify(ok=True, message="已保存。录制参数将在当前录制全部结束后自动应用；编码参数从下一个转码任务生效。")

    @app.get("/api/proxy")
    def proxy():
        with config.lock:
            return jsonify(**config.proxy_public(), revision=config.revision())

    @app.put("/api/proxy")
    def save_proxy():
        config.save_proxy(body())
        return jsonify(ok=True, message="代理设置已保存，下次检测直播间时生效。")

    return app
