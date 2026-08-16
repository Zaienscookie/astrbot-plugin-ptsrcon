"""AstrBot 日志字段注入：修复 'Formatting field not found in record: plugin_tag' 报错。

AstrBot 的日志 Formatter 依赖每条 LogRecord 携带 plugin_tag 等自定义字段，
而这些字段由框架内部的 _RecordEnricherFilter 注入。插件自建 logger 的记录
在某些队列路径下会绕过该 Filter，导致格式化缺字段抛异常。
这里为插件的 logger 统一补上这些字段（与框架规则一致）。
"""
import logging
from pathlib import Path

_LEVEL_SHORT = {"DEBUG": "D", "INFO": "I", "WARNING": "W", "ERROR": "E", "CRITICAL": "C"}


class _AstrBotEnricher(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "plugin_tag"):
            record.plugin_tag = "[Plug]"
        if not hasattr(record, "short_levelname"):
            record.short_levelname = _LEVEL_SHORT.get(record.levelname, record.levelname[:1] or "I")
        if not hasattr(record, "astrbot_version_tag"):
            record.astrbot_version_tag = " [v?]" if record.levelno >= logging.WARNING else ""
        if not hasattr(record, "source_file"):
            record.source_file = Path(record.pathname).name
        if not hasattr(record, "source_line"):
            record.source_line = record.lineno
        if not hasattr(record, "is_trace"):
            record.is_trace = record.name == "astrbot.trace"
        if not hasattr(record, "ansi_prefix"):
            record.ansi_prefix = ""
        if not hasattr(record, "ansi_reset"):
            record.ansi_reset = ""
        return True


def patch_logger(name: str) -> logging.Logger:
    """创建并返回已注入 AstrBot 日志字段的 logger。"""
    logger = logging.getLogger(name)
    logger.addFilter(_AstrBotEnricher())
    return logger
