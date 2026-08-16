# AstrBot 插件 ptsrcon — 群服互通服务端

AstrBot 侧「QQ群 ↔ Minecraft 服务器」互通服务端：
- **WebSocket 服务端**（默认 `0.0.0.0:8765`）：接收 Forge Mod 客户端连接并认证
- **游戏事件转发**：chat/join/leave/death/achievement/server_start/server_stop → 绑定 QQ 群
- **远程命令**：群里发 `@bot srcon <服务器> <命令>` 执行游戏内命令
- **白名单**：按 QQ 号 + 可操作服务器列表控制，管理员放行

对应 Mod：[srcon-mod](https://github.com/Zaienscookie/srcon-mod)（Minecraft 1.20.1 Forge）

## 安装
1. 将本目录放到 `AstrBot/data/plugins/astrbot_plugin_ptsrcon/`
2. 依赖：`pip install astrbot websockets pyyaml`
3. 编辑 `config.yaml`：
   - **`token`**：改成高强度随机串，与 Mod 端 `SRCON_TOKEN` 保持一致
   - **`whitelist`**：填入允许执行命令的 QQ 号及其可操作服务器（`*` 表示全部）
   - **`forward_groups`**：配置事件转发到哪些群，如 `s1 -> ["251972336"]`
4. 重启 AstrBot，确认日志出现 `[SRCon] 插件初始化完成`

## 使用
```
srcon list                    # 查看已连接的服务器
srcon s1 say 大家好           # 在 s1 服执行命令（白名单内）
srcon s1 give Steve diamond 64
```

## 配置项
| 键 | 默认 | 说明 |
|----|------|------|
| `ws_host` / `ws_port` | `0.0.0.0` / `8765` | WebSocket 监听地址 |
| `token` | 空 | 认证 token，与 Mod 的 `SRCON_TOKEN` 一致 |
| `admin_qq` | 空 | 管理员 QQ，无视白名单 |
| `whitelist` | `{}` | `{"QQ号": ["s1", "s2"]}`，`*` 通配全部服务器 |
| `forward_groups` | `{}` | `{"服务器id": ["群号"]}` 事件转发绑定 |
| `forward_event_types` | 全事件 | 逗号分隔要转发的类型 |
| `chat_format` | `{player} › {msg}` | 聊天转发格式 |
| `forward_with_server_prefix` | `true` | 是否加 `[服务器名]` 前缀 |
| `session_prefix` | `aiocqhttp:group_message:` | 主动发消息的 session 前缀（按平台改） |

## 通信协议摘要
Mod → 插件（JSON）：
```json
{"type":"auth","server":"s1","server_name":"生存服","token":"xxx"}
{"type":"chat","server":"s1","server_name":"生存服","player":"Steve","msg":"大家好"}
```
插件 → Mod：
```json
{"type":"command","server":"s1","command":"say hi","ack_id":"..."}
{"type":"command_ack","ack_id":"...","ok":true,"output":"..."}
```
