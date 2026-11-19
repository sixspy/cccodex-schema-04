import json
from pathlib import Path

from aiohttp import web
from itsdangerous import BadSignature, URLSafeSerializer

from app.config import Config
from app.db import init_db
from app.services import (
    apply_contract,
    card_view,
    decide,
    list_contracts,
    list_directory,
    list_reports,
    list_watermarks,
    open_card,
    parent_detail,
    refreeze,
    remove_edge,
    run_breaking_check,
    run_reconcile,
    upsert_edge,
    waterline,
)
from app.services.errors import ServiceError

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
serializer = URLSafeSerializer(Config.SECRET, salt="cc-schema-session")


def create_app(db_path: str | None = None) -> web.Application:
    if db_path:
        import os

        os.environ["DATABASE_PATH"] = db_path
    init_db(db_path)
    app = web.Application()
    app["db_path"] = db_path
    app.router.add_static("/static/", STATIC)
    app.router.add_get("/", root)
    app.router.add_get("/login", login_page)
    app.router.add_get("/app/", spa_page)
    app.router.add_post("/api/login", api_login)
    app.router.add_post("/api/logout", api_logout)
    app.router.add_get("/api/meta", api_meta)
    app.router.add_get("/api/contracts", api_contracts)
    app.router.add_post("/api/contracts/{cid}/breaking-check", api_breaking)
    app.router.add_post("/api/contracts/{cid}/apply", api_apply)
    app.router.add_get("/api/contracts/{cid}/upgrade-card", api_upgrade_card)
    app.router.add_get("/api/contracts/{cid}/card", api_card_view)
    app.router.add_post("/api/contracts/{cid}/lineage/refreeze", api_refreeze)
    app.router.add_post("/api/contracts/{cid}/lineage/decide", api_decide)
    app.router.add_get("/api/lineage/edges", api_edges)
    app.router.add_post("/api/lineage/edges", api_add_edge)
    app.router.add_delete("/api/lineage/edges", api_del_edge)
    app.router.add_get("/api/lineage/parents/{name}", api_parent)
    app.router.add_get("/api/lineage/watermarks", api_watermarks)
    app.router.add_get("/api/lineage/waterline", api_waterline)
    app.router.add_get("/api/reconcile-reports", api_reports)
    app.router.add_post("/api/reconcile-reports", api_reconcile)
    return app


def _session(request: web.Request) -> dict:
    raw = request.cookies.get("schema_session")
    if not raw:
        return {}
    try:
        data = serializer.loads(raw)
        return data if isinstance(data, dict) else {}
    except BadSignature:
        return {}


def require_user(request: web.Request) -> str:
    user = _session(request).get("user")
    if user != Config.ADMIN_USER:
        raise ServiceError("未登录", 401)
    return user


def json_resp(data: dict, status: int = 200) -> web.Response:
    return web.Response(
        status=status,
        text=json.dumps(data, ensure_ascii=False),
        content_type="application/json",
    )


async def root(request: web.Request):
    if _session(request).get("user") == Config.ADMIN_USER:
        raise web.HTTPFound("/app/")
    raise web.HTTPFound("/login")


async def login_page(_request: web.Request):
    return web.FileResponse(STATIC / "login.html")


async def spa_page(request: web.Request):
    if _session(request).get("user") != Config.ADMIN_USER:
        raise web.HTTPFound("/login")
    return web.FileResponse(STATIC / "index.html")


async def api_login(request: web.Request):
    body = await request.json()
    user = str(body.get("username", "")).strip()
    pwd = str(body.get("password", ""))
    if user != Config.ADMIN_USER or pwd != Config.ADMIN_PASSWORD:
        return json_resp({"ok": False, "error": "账号或密码错误"}, 400)
    resp = json_resp({"ok": True, "user": user})
    resp.set_cookie(
        "schema_session",
        serializer.dumps({"user": user}),
        httponly=True,
        path="/",
    )
    return resp


async def api_logout(_request: web.Request):
    resp = json_resp({"ok": True})
    resp.del_cookie("schema_session", path="/")
    return resp


async def api_meta(request: web.Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)
    return json_resp({"lab": Config.LAB_NAME, "product": "cc-schema-lab"})


async def api_contracts(request: web.Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)
    return json_resp({"items": list_contracts(request.app.get("db_path"))})


async def api_breaking(request: web.Request):
    try:
        require_user(request)
        cid = int(request.match_info["cid"])
        return json_resp({"check": run_breaking_check(cid, request.app.get("db_path"))})
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)


async def api_apply(request: web.Request):
    try:
        require_user(request)
        cid = int(request.match_info["cid"])
        return json_resp({"contract": apply_contract(cid, request.app.get("db_path"))})
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)


async def api_upgrade_card(request: web.Request):
    try:
        user = require_user(request)
        cid = int(request.match_info["cid"])
        return json_resp({"card": open_card(cid, user, request.app.get("db_path"))})
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)


async def api_refreeze(request: web.Request):
    try:
        user = require_user(request)
        cid = int(request.match_info["cid"])
        return json_resp({"card": refreeze(cid, user, request.app.get("db_path"))})
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)


async def api_decide(request: web.Request):
    try:
        user = require_user(request)
        cid = int(request.match_info["cid"])
        result = decide(cid, user, request.app.get("db_path"))
        result["card"] = card_view(cid, request.app.get("db_path"))
        return json_resp(result)
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)


async def api_card_view(request: web.Request):
    try:
        require_user(request)
        cid = int(request.match_info["cid"])
        return json_resp({"card": card_view(cid, request.app.get("db_path"))})
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)


async def api_edges(request: web.Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)
    return json_resp({"items": list_directory(request.app.get("db_path"))})


async def api_watermarks(request: web.Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)
    child = request.query.get("child_table") or None
    return json_resp({"items": list_watermarks(child, request.app.get("db_path"))})


async def api_waterline(request: web.Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)
    return json_resp({"items": waterline(request.app.get("db_path"))})


async def api_add_edge(request: web.Request):
    try:
        user = require_user(request)
        body = await request.json()
        return json_resp(
            {
                "result": upsert_edge(
                    str(body.get("child_table", "")),
                    str(body.get("child_field", "")),
                    str(body.get("parent_table", "")),
                    str(body.get("parent_field", "")),
                    user,
                    request.app.get("db_path"),
                )
            }
        )
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)


async def api_del_edge(request: web.Request):
    try:
        user = require_user(request)
        body = await request.json()
        return json_resp(
            {
                "result": remove_edge(
                    str(body.get("child_table", "")),
                    str(body.get("child_field", "")),
                    str(body.get("parent_table", "")),
                    str(body.get("parent_field", "")),
                    user,
                    request.app.get("db_path"),
                )
            }
        )
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)


async def api_parent(request: web.Request):
    try:
        require_user(request)
        name = request.match_info["name"]
        return json_resp({"parent": parent_detail(name, request.app.get("db_path"))})
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)


async def api_reports(request: web.Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)
    return json_resp({"items": list_reports(request.app.get("db_path"))})


async def api_reconcile(request: web.Request):
    try:
        require_user(request)
        body = await request.json()
        as_of = str(body.get("as_of") or "").strip() or "2026-11-18"
        return json_resp({"report": run_reconcile(as_of, request.app.get("db_path"))})
    except ServiceError as exc:
        return json_resp({"error": exc.message}, exc.status_code)
