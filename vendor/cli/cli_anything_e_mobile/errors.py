"""cli_anything_e_mobile 的异常类型。"""

from __future__ import annotations


class EMError(RuntimeError):
    """基础异常。"""


class GUIError(EMError):
    """GUI(CDP)操作失败。"""


class ConfigValidationError(EMError):
    """配置不合法。"""
