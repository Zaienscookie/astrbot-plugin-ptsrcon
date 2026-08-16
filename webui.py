"""SRCon WebUI — 群服互通管理面板（aiohttp，纯异步，无需 uvicorn）

- GET  /api/status      服务器连接状态
- GET  /api/logs        最近事件日志
- GET  /api/whitelist   白名单
- POST /api/whitelist   保存白名单（全量覆盖，写回 config.yaml）
- POST /api/command     下发服务器命令
- GET  /api/config      配置概览（敏感字段打码）
- GET  /                前端页面（index.html）
"""
import logging
from .logpatch import patch_logger
import time
from pathlib import Path
from typing import Optional

import yaml
from aiohttp import web

logger = patch_logger('astrbot.plugin.ptsrcon.webui')


class SrconWebUI:
    def __init__(self, host: str, port: int, token: str, plugin, static_dir: str | Path,
                 config_path: str | Path):
        self.host = host
        self.port = port
        self._token = token
        self._plugin = plugin
        self._config_path = Path(config_path)
        self._static_dir = Path(static_dir)
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self.app = web.Application(middlewares=[self._make_auth_middleware()])
        self._setup_routes()

    # ────────── 服务 ──────────

    async def run(self):
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        logger.info(f"[SRCon] WebUI 已启动: http://{self.host}:{self.port}/")

    async def stop(self):
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    # ────────── 认证中间件 ──────────

    def _make_auth_middleware(self):
        @web.middleware
        async def auth(request: web.Request, handler):
            path = request.path
            if path in ("/", "/index.html", "/favicon.ico"):
                return await handler(request)
            token = request.headers.get("x-srcon-token", "")
            if not self._token or token != self._token:
                return web.json_response({"error": "unauthorized"}, status=401)
            return await handler(request)
        return auth

    # ────────── 路由 ──────────

    def _setup_routes(self):
        app = self.app
        app.router.add_get("/", self._index)
        app.router.add_get("/index.html", self._index)
        app.router.add_get("/api/status", self._api_status)
        app.router.add_get("/api/logs", self._api_logs)
        app.router.add_get("/api/whitelist", self._api_get_whitelist)
        app.router.add_post("/api/whitelist", self._api_save_whitelist)
        app.router.add_post("/api/command", self._api_command)
        app.router.add_get("/api/config", self._api_config)

    async def _index(self, request: web.Request):
        f = self._static_dir / "index.html"
        if not f.exists():
            return web.json_response({"error": "前端文件缺失"}, status=500)
        return web.FileResponse(f)

    async def _api_status(self, request: web.Request):
        servers = self._plugin.ws.servers
        return web.json_response({
            "ws_listening": True,
            "server_count": len(servers),
            "servers": [
                {"id": sid, "name": info["name"], "connected": info["connected"]}
                for sid, info in servers.items()
            ],
            "uptime": time.time() - self._plugin.started_at if getattr(self._plugin, "started_at", 0) else None,
        })

    async def _api_logs(self, request: web.Request):
        try:
            limit = int(request.query.get("limit", "50"))
        except ValueError:
            limit = 50
        limit = max(1, min(limit, 300))
        log = list(self._plugin.ws.event_log)[-limit:]
        return web.json_response({"logs": [dict(e) for e in log]})

    async def _api_get_whitelist(self, request: web.Request):
        cfg = self._read_config()
        return web.json_response({
            "whitelist": cfg.get("whitelist", {}) or {},
            "admin_qq": cfg.get("admin_qq", "") or "",
        })

    async def _api_save_whitelist(self, request: web.Request):
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "无效 JSON"}, status=400)
        new_wl = body.get("whitelist")
        if not isinstance(new_wl, dict):
            return web.json_response({"error": "whitelist 必须是 {QQ: [服务器]}"}, status=400)
        cfg = self._read_config()
        cfg["whitelist"] = new_wl
        if "admin_qq" in body:
            cfg["admin_qq"] = str(body.get("admin_qq", ""))
        # 备份后写回
        if self._config_path.exists():
            bak = self._config_path.with_suffix(".yaml.bak")
            try:
                bak.write_bytes(self._config_path.read_bytes())
            except Exception:  # noqa: BLE001
                pass
        with open(self._config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
        # 热更新插件内存配置
        self._plugin.config = cfg
        return web.json_response({
            "ok": True,
            "whitelist": new_wl,
            "admin_qq": cfg.get("admin_qq", "") or "",
        })

    async def _api_command(self, request: web.Request):
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "无效 JSON"}, status=400)
        server_id = str(body.get("server", "")).strip()
        cmd = str(body.get("command", "")).strip().lstrip("/")
        if not server_id or not cmd:
            return web.json_response({"error": "需要 server 和 command"}, status=400)
        ok, info = await self._plugin.ws.send_command(server_id, cmd)
        return web.json_response({"ok": ok, "message": info})

    async def _api_config(self, request: web.Request):
        cfg = self._read_config()
        mask = lambda v: (str(v)[:4] + "****") if v else ""
        return web.json_response({
            "ws_host": cfg.get("ws_host", "0.0.0.0"),
            "ws_port": cfg.get("ws_port", 8766),
            "token_masked": mask(cfg.get("token", "")),
            "webui_token_masked": mask(cfg.get("webui_token", "")),
            "admin_qq": cfg.get("admin_qq", "") or "",
            "session_prefix": cfg.get("session_prefix", "aiocqhttp:group_message:"),
            "forward_groups": cfg.get("forward_groups", {}),
            "forward_event_types": cfg.get("forward_event_types", ""),
            "forward_with_server_prefix": cfg.get("forward_with_server_prefix", True),
            "chat_format": cfg.get("chat_format", "{player} › {msg}"),
        })

    # ────────── 工具 ──────────

    def _read_config(self) -> dict:
        if self._config_path.exists():
            try:
                with open(self._config_path, "r", encoding="utf-8") as f:
                    return yaml.safe_load(f) or {}
            except Exception:  # noqa: BLE001
                logger.warning("[SRCon] config.yaml 读取失败，返回内存配置")
        return dict(self._plugin.config or {})