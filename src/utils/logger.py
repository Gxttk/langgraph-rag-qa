"""统一日志：全项目从这里导入 logger，用 loguru 替代散落各处的 print 调试。"""
from loguru import logger

from ..config import settings

logger.remove()
logger.add(
    __import__("sys").stderr,
    level=settings.log_level.upper(),
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | "
    "<cyan>{name}</cyan>:<cyan>{line}</cyan> - {message}",
)

__all__ = ["logger"]
