"""群服互通 SRCon — AstrBot 插件
- WebSocket 服务端：接收 Forge Mod 连接（chat/join/leave/death/achievement 事件 + command 下发）
- srcon 指令：QQ 群远程执行 Minecraft 服务器命令
- 白名单：按 QQ 号 + 可操作服务器列表控制
- 事件转发：游戏内事件按群绑定转发到 QQ 群
"""
import asyncio
import logging
from .logpatch import patch_logger
import time
from pathlib import Path

from astrbot.api import AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.filter.event_message_type import EventMessageType

from .ws_server import SrconWSServer
from .webui import SrconWebUI

logger = patch_logger('astrbot.plugin.ptsrcon')


def _cfg(config, key, default=""):
    if not config:
        return default
    v = config.get(key)
    return v if v is not None else default


@register("ptsrcon", "zains", "群服互通：QQ群 ↔ Minecraft 服务器（WebSocket）", "v1.0.0")
class SrconPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        # 合并插件目录 config.yaml（AstrBot 传参优先）
        self.config = config or {}
        if not self.config:
            cfg_file = Path(__file__).parent / "config.yaml"
            if cfg_file.exists():
                try:
                    import yaml
                    with open(cfg_file, "r", encoding="utf-8") as f:
                        self.config = yaml.safe_load(f) or {}
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[SRCon] config.yaml 解析失败: {exc}")
        self.ws = SrconWSServer(
            host=str(_cfg(self.config, "ws_host", "0.0.0.0")),
            port=int(_cfg(self.config, "ws_port", 8766) or 8766),
            token=str(_cfg(self.config, "token", "")),
            on_event=self._on_ws_event,
        )
        self._session_cache: dict[str, str] = {}  # group_id -> unified_msg_origin
        self._task = None
        self.started_at: float = time.time()
        # WebUI 管理面板
        self.webui = None
        try:
            if bool(_cfg(self.config, "webui_enabled", False) or False):
                self.webui = SrconWebUI(
                    host=str(_cfg(self.config, "webui_host", "0.0.0.0")),
                    port=int(_cfg(self.config, "webui_port", 18766) or 18766),
                    token=str(_cfg(self.config, "webui_token", "") or ""),
                    plugin=self,
                    static_dir=Path(__file__).parent / "webui",
                    config_path=Path(__file__).parent / "config.yaml",
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[SRCon] WebUI 初始化失败: {exc}")

    # ──────────── 生命周期 ────────────

    async def initialize(self):
        self._task = asyncio.create_task(self._ws_runner(), name="ptsrcon-ws-server")
        port = int(_cfg(self.config, "ws_port", 8766) or 8766)
        logger.info(f"[SRCon] 插件初始化完成，WebSocket 服务端监听 :{port}")
        if self.webui:
            self._webui_task = asyncio.create_task(self._run_webui(), name="ptsrcon-webui")
            wport = int(_cfg(self.config, "webui_port", 18766) or 18766)
            logger.info(f"[SRCon] WebUI 已启动: http://0.0.0.0:{wport}/")

    async def _ws_runner(self):
        try:
            await self.ws.start()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[SRCon] WebSocket 服务端启动失败: {exc}")

    async def _run_webui(self):
        try:
            await self.webui.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[SRCon] WebUI 运行异常: {exc}")

    async def terminate(self):
        if getattr(self, "_webui_task", None):
            self._webui_task.cancel()
            try:
                await self._webui_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self.webui:
            try:
                await self.webui.stop()
            except Exception:  # noqa: BLE001
                pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self.ws.stop()

    # ──────────── 事件转发 ────────────

    def _forward_groups_for(self, server_id: str) -> list[str]:
        fg = _cfg(self.config, "forward_groups", {}) or {}
        groups = list(fg.get(server_id, []) or [])
        if not groups:
            fallback = str(_cfg(self.config, "forward_all_groups", "") or "")
            groups = [g.strip() for g in fallback.split(",") if g.strip()]
        return [str(g) for g in groups]

    def _allowed_event_types(self) -> set[str]:
        raw = str(_cfg(
            self.config, "forward_event_types",
            "chat,join,leave,death,achievement,server_start,server_stop",
        ) or "")
        return {t.strip() for t in raw.split(",") if t.strip()}

    async def _on_ws_event(self, msg: dict):
        mtype = msg.get("type", "")
        if mtype not in self._allowed_event_types():
            return
        text = self._format_event(msg)
        if not text:
            return
        if _cfg(self.config, "forward_with_server_prefix", True):
            name = msg.get("server_name") or msg.get("server") or ""
            text = f"[{name}] {text}" if name else text
        for gid in self._forward_groups_for(msg.get("server", "")):
            try:
                await self._send_to_group(gid, text)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[SRCon] 转发到群 {gid} 失败: {exc}")

    def _format_event(self, msg: dict) -> str:
        mtype = msg.get("type")
        player = msg.get("player", "")
        if mtype == "chat":
            fmt = str(_cfg(self.config, "chat_format", "{player} › {msg}") or "")
            return fmt.format(
                player=player,
                msg=msg.get("msg", ""),
                server_name=msg.get("server_name", ""),
            )
        if mtype == "join":
            return f"🟢 {player} 加入了游戏"
        if mtype == "leave":
            return f"🔴 {player} 退出了游戏"
        if mtype == "death":
            return f"💀 {player} 死亡：{msg.get('reason', '')}"
        if mtype == "achievement":
            return f"🏆 {player} 达成成就：{msg.get('title', '')}"
        if mtype == "server_start":
            return "🟢 服务器已启动"
        if mtype == "server_stop":
            return "🔴 服务器已关闭"
        return ""

    async def _send_to_group(self, group_id: str, text: str):
        umo = self._session_cache.get(str(group_id))
        if not umo:
            prefix = str(_cfg(self.config, "session_prefix", "aiocqhttp:GroupMessage:"))
            # 兼容旧配置的小写 group_message：MessageType 枚举值是 GroupMessage（驼峰）
            prefix = prefix.replace("group_message:", "GroupMessage:")
            umo = f"{prefix}{group_id}"
        await self.context.send_message(umo, MessageChain().message(text))

    # ──────────── 群聊 → 游戏转发 ────────────

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def _forward_group_chat(self, event: AstrMessageEvent):
        gid = str(event.get_group_id() or "")
        if gid:
            self._session_cache[gid] = event.unified_msg_origin

        # 群→游戏白名单：只允许 forward_groups 中配置的群将消息转发到游戏
        fg = _cfg(self.config, "forward_groups", {}) or {}
        allowed_groups: set[str] = set()
        for groups in fg.values():
            if groups:
                allowed_groups.update(str(g) for g in groups)
        if gid and allowed_groups and gid not in allowed_groups:
            return

        if not bool(_cfg(self.config, "group_to_game_enable", True)):
            return
        # 转发模式：all=全部群消息 | wake=仅@机器人/z唤醒触发 | off=关闭
        mode = str(_cfg(self.config, "group_chat_mode", "all") or "all").lower()
        if mode == "off":
            return
        if mode == "wake" and not event.is_at_or_wake_command:
            return
        text = (event.message_str or "").strip()
        if not text:
            return
        low = text.lower()
        if low.startswith("srcon") or low.startswith("/srcon"):
            return
        content = text
        try:
            sender = event.get_sender_name() or str(event.get_sender_id())
        except Exception:  # noqa: BLE001
            sender = str(event.get_sender_id())
        fmt = str(_cfg(self.config, "group_chat_format", "[QQ] {player} › {msg}") or "")
        formatted = fmt.format(player=sender, msg=content) if fmt else f"[QQ] {sender} › {content}"
        try:
            n = await self.ws.broadcast_group_chat(formatted)
            logger.info(f"[SRCon] 群聊→游戏 已广播到 {n} 台服务器: {formatted}")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[SRCon] 群聊→游戏 广播失败: {exc}")

    # ──────────── 白名单 ────────────

    def _is_admin(self, user_id) -> bool:
        admin = str(_cfg(self.config, "admin_qq", "") or "")
        return bool(admin) and str(user_id) == admin

    def _check_whitelist(self, user_id, server_id: str) -> bool:
        if self._is_admin(user_id):
            return True
        wl = _cfg(self.config, "whitelist", {}) or {}
        servers = wl.get(str(user_id))
        if servers is None:
            return False
        return "*" in servers or server_id in servers

    # ──────────── 命令 ────────────

    @filter.command("srcon")
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def srcon(self, event: AstrMessageEvent):
        gid = str(event.get_group_id() or "")
        if gid:
            self._session_cache[gid] = event.unified_msg_origin

        text = (event.message_str or "").strip()
        rest = text[len("srcon"):].strip() if text.lower().startswith("srcon") else text
        parts = rest.split(maxsplit=1)
        if not parts:
            yield event.plain_result(self._help_text())
            return

        action = parts[0].lower()
        if action == "list":
            servers = self.ws.servers
            if not servers:
                yield event.plain_result("📡 当前没有已连接的服务器")
                return
            lines = ["📡 已连接服务器："]
            for sid, info in servers.items():
                lines.append(f"  • {sid}（{info['name']}）")
            yield event.plain_result("\n".join(lines))
            return
        if action == "help":
            yield event.plain_result(self._help_text())
            return

        # 视为 server cmd
        if len(parts) < 2:
            yield event.plain_result("用法：srcon <服务器名> <命令>，例如 srcon s1 say 大家好")
            return
        server_id, cmd = parts[0], parts[1].lstrip("/")
        user_id = str(event.get_sender_id())
        if not self._check_whitelist(user_id, server_id):
            yield event.plain_result(f"⛔ 你无权操作服务器 [{server_id}]")
            return
        ok, info = await self.ws.send_command(server_id, cmd)
        yield event.plain_result(("✅ " if ok else "❌ ") + info)

    def _help_text(self) -> str:
        return (
            "📡 群服互通 SRCon\n"
            "• srcon list — 查看已连接服务器\n"
            "• srcon <服务器> <命令> — 远程执行命令\n"
            "  例：srcon s1 say 大家好\n"
            "  例：srcon s1 give Steve diamond 64"
        )
