"""平台统一异常类型。

玻璃熔窑现场必须区分两类失败：一类是投料、点火、成型、退火的时序被破坏
（工程接线错误，装机阶段就要发现），另一类是联锁条件不满足（运行期可恢复，
设备状态满足后就能继续）。审计按异常类型分别落盘，巡检时才能一眼分辨
"顺序错"与"条件没到"。
"""

from __future__ import annotations


class GlassMeltError(Exception):
    """所有平台异常的基类，携带出错的组件名。"""

    def __init__(self, message: str, *, component: str = "glassmelt") -> None:
        super().__init__(message)
        self.message = message
        self.component = component

    def as_record(self) -> dict:
        """转成审计日志可以直接落盘的一条记录。"""

        return {
            "type": type(self).__name__,
            "component": self.component,
            "message": self.message,
        }


class SequenceError(GlassMeltError):
    """投料、点火、成型或退火的顺序被破坏。"""


class InterlockError(GlassMeltError):
    """联锁条件不满足，动作被拒绝。"""


class StorageError(GlassMeltError):
    """持久化读写失败。"""


class ConfigError(GlassMeltError):
    """配置缺失或取值非法。"""


class NamespaceError(GlassMeltError):
    """命名空间重复注册或解析失败。"""
