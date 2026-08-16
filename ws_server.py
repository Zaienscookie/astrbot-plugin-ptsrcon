"""SRCon WebSocket 服务端
接收 Forge Mod 连接，处理认证、事件上报，提供命令下发与服务器列表。
"""
import asyncio
import json
import time
from collections import deque
import logging
from .logpatch import patch_logger
import time

import websockets

logger = patch_logger('astrbot.plugin.ptsrcon.ws')


class SrconWSServer:
    """WebSocket 服务端：每个连接的 Mod 代表一台 Minecraft 服务器。"""

    def __init__(self, host: str, port: int, token: str, on_event=None):
        self.host = host
        self.port = port
        self.token = token or ""
        # on_event: async callable(dict) 收到事件时回调（chat/join/leave/death/...）
        self.on_event = on_event
        self._server = None
        self._servers: dict[str, dict] = {}  # server_id -> {"ws": ws, "name": str, "connected_at": float}
        self.event_log: deque[dict] = deque(maxlen=300)  # 最近事件环形缓冲（供 WebUI 展示）

    @property
    def servers(self) -> dict[str, dict]:
        """线程安全快照：server_id -> {name, connected}"""
        return {sid: {"name": sv["name"], "connected": True} for sid, sv in self._servers.items()}

    async def start(self) -> None:
        self._server = await websockets.serve(
            self._handler,
            self.host,
            self.port,
            ping_interval=20,
            ping_timeout=60,
            max_size=2 * 1024 * 1024,
        )
        logger.info(f"SRCon WebSocket 服务端已启动: ws://{self.host}:{self.port}")

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None
        # 断开所有连接
        for sid in list(self._servers.keys()):
            try:
                await self._servers[sid]["ws"].close()
            except Exception:
                pass
        self._servers.clear()
        logger.info("SRCon WebSocket 服务端已停止")

    async def _handler(self, ws) -> None:
        try:
            await self._handler_inner(ws)
        except asyncio.CancelledError:
            raise
        except Exception:
            import traceback as _tb
            try:
                with open("/tmp/ws_err.log", "a", encoding="utf-8") as _f:
                    _f.write(_tb.format_exc())
            except Exception:
                pass
            logger.exception("[SRCon] 连接处理异常(已捕获)")

    async def _handler_inner(self, ws) -> None:
        """处理单个 Mod 连接。"""
        peer = getattr(ws, "remote_address", None)
        server_id = None
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    logger.warning(f"[SRCon] 来自 {peer} 的非 JSON 消息: {str(raw)[:100]}")
                    continue
                if not isinstance(msg, dict):
                    continue
                mtype = msg.get("type")
                if mtype == "auth":
                    ok = self._handle_auth(ws, msg)
                    if ok:
                        server_id = msg.get("server") or f"unknown_{getattr(peer, 'host', peer)}"
                    await ws.send(json.dumps(
                        {"type": "auth_result", "ok": ok,
                         "error": None if ok else "token 错误或缺少 server 字段"},
                        ensure_ascii=False,
                    ))
                    if not ok:
                        await ws.close()
                        return
                elif mtype in (
                    "chat", "join", "leave", "death", "achievement",
                    "server_start", "server_stop", "command_result",
                ):
                    if server_id is None:
                        # 未认证先发了事件，尝试用消息里的 server 字段
                        server_id = msg.get("server")
                        if server_id and server_id not in self._servers:
                            self._servers[server_id] = {
                                "ws": ws,
                                "name": msg.get("server_name") or server_id,
                                "connected_at": time.time(),
                            }
                    msg["server"] = msg.get("server") or server_id
                    self.event_log.append({"ts": time.time(), "origin": "server", **dict(msg)})
                    if self.on_event:
                        try:
                            await self.on_event(dict(msg))
                        except Exception as exc:  # noqa: BLE001
                            logger.error(f"[SRCon] 事件回调失败: {exc}")
        except websockets.exceptions.ConnectionClosed as exc:
            logger.info(f"[SRCon] 连接关闭: {peer} code={exc.code} reason={exc.reason}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[SRCon] 连接异常: {peer} {exc}")
        finally:
            if server_id and server_id in self._servers:
                sv = self._servers.pop(server_id, None)
                if sv:
                    stop_msg = {
                        "type": "server_stop",
                        "server": server_id,
                        "server_name": sv["name"],
                    }
                    self.event_log.append({"ts": time.time(), "origin": "server", **stop_msg})
                if sv and self.on_event:
                    try:
                        await self.on_event(stop_msg)
                    except Exception:
                        pass
                logger.info(f"[SRCon] 服务器 [{server_id}] 已断开")

    def _handle_auth(self, ws, msg: dict) -> bool:
        """认证成功返回 True，并注册服务器连接。"""
        if self.token and msg.get("token") != self.token:
            logger.warning(f"[SRCon] 认证失败: token 不匹配 (来自 {ws.remote_address})")
            return False
        server_id = str(msg.get("server") or "").strip()
        if not server_id:
            return False
        server_name = str(msg.get("server_name") or server_id).strip()
        # 如果该 server 已连接，先顶掉旧连接
        old = self._servers.pop(server_id, None)
        if old and old["ws"] != ws:
            try:
                asyncio.create_task(old["ws"].close())
            except Exception:
                pass
        self._servers[server_id] = {
            "ws": ws,
            "name": server_name,
            "connected_at": time.time(),
        }
        logger.info(f"[SRCon] 服务器 [{server_id}] ({server_name}) 已认证连接")
        return True

    async def send_command(self, server_id: str, command: str, ack_id: str | None = None) -> tuple[bool, str]:
        """向指定服务器发送命令。返回 (成功?, 提示信息)。"""
        sv = self._servers.get(server_id)
        if sv is None:
            self.event_log.append({"ts": time.time(), "origin": "web", "type": "command_failed",
                                   "server": server_id, "cmd": command,
                                   "reason": "服务器未连接"})
            return False, f"服务器 [{server_id}] 未连接（可用 srcon list 查看）"
        payload: dict = {"type": "command", "cmd": command}
        if ack_id:
            payload["ack_id"] = ack_id
        try:
            await sv["ws"].send(json.dumps(payload, ensure_ascii=False))
            self.event_log.append({"ts": time.time(), "origin": "web", "type": "command",
                                   "server": server_id, "cmd": command})
            return True, f"已发送至 [{server_id}]：/{command}"
        except Exception as exc:  # noqa: BLE001
            self.event_log.append({"ts": time.time(), "origin": "web", "type": "command_failed",
                                   "server": server_id, "cmd": command, "reason": str(exc)})
            return False, f"发送失败: {exc}"

    async def broadcast_group_chat(self, text: str) -> int:
        """向所有在线服务器广播群聊消息（游戏内显示）。返回发送数。"""
        payload = {"type": "group_chat", "msg": text}
        n = 0
        for sid, sv in list(self._servers.items()):
            try:
                await sv["ws"].send(json.dumps(payload, ensure_ascii=False))
                n += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[SRCon] 群聊广播到 {sid} 失败: {exc}")
        return n

    def is_connected(self, server_id: str) -> bool:
        return server_id in self._servers
