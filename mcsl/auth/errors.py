"""认证异常：全部带中文可读信息，UI 直接显示 message。"""
from __future__ import annotations


class AuthError(Exception):
    """认证相关错误基类。"""

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def __str__(self) -> str:  # pragma: no cover - 展示用
        return f"{self.message}（{self.detail}）" if self.detail else self.message


class OAuthCancelled(AuthError):
    """用户取消 / 超时。"""

    def __init__(self, message: str = "登录已取消或超时") -> None:
        super().__init__(message)


class OAuthError(AuthError):
    """微软 OAuth 端点返回的错误。"""

    def __init__(self, code: str, description: str) -> None:
        super().__init__(f"微软登录失败：{description or code}", detail=code)
        self.code = code
        self.description = description


class XboxError(AuthError):
    """Xbox Live / XSTS 环节错误。"""

    def __init__(self, message: str, *, xerr: int | None = None, redirect: str = "") -> None:
        super().__init__(message, detail=f"XErr={xerr}" if xerr else "")
        self.xerr = xerr
        self.redirect = redirect


class MinecraftAuthError(AuthError):
    """Minecraft Services 环节错误（如未持有游戏、档案不存在）。"""


class NetworkError(AuthError):
    """网络层错误（连不上、超时）。"""
