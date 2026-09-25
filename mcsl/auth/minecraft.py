"""Minecraft Services：用 XSTS 令牌换 Minecraft access token，并取档案。"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests

from .errors import MinecraftAuthError, NetworkError
from .xbox import XstsToken

log = logging.getLogger(__name__)

LOGIN_URL = "https://api.minecraftservices.com/authentication/login_with_xbox"
PROFILE_URL = "https://api.minecraftservices.com/minecraft/profile"
ENTITLEMENTS_URL = "https://api.minecraftservices.com/entitlements/mcstore"


@dataclass
class McToken:
    access_token: str
    token_type: str
    expires_at: float   # unix 秒
    username_hint: str = ""   # login_with_xbox 返回的账号 hash，不是玩家名


@dataclass
class Profile:
    uuid: str
    name: str


def login_with_xbox(xsts: XstsToken, session: requests.Session) -> McToken:
    identity = f"XBL3.0 x={xsts.uhs};{xsts.token}"
    try:
        resp = session.post(LOGIN_URL, json={"identityToken": identity}, timeout=30,
                            headers={"Accept": "application/json"})
    except requests.RequestException as exc:
        raise NetworkError("无法连接 Minecraft 服务", detail=str(exc)) from exc
    if resp.status_code >= 400:
        detail = resp.text[:200]
        raise MinecraftAuthError(f"Minecraft 登录失败（HTTP {resp.status_code}）", detail=detail)
    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise MinecraftAuthError("Minecraft 登录响应中没有 access_token", detail=str(data)[:200])
    return McToken(
        access_token=token,
        token_type=data.get("token_type", "Bearer"),
        expires_at=time.time() + int(data.get("expires_in") or 86400),
        username_hint=data.get("username", ""),
    )


def fetch_profile(mc: McToken, session: requests.Session) -> Profile:
    try:
        resp = session.get(PROFILE_URL, timeout=30,
                           headers={"Authorization": f"{mc.token_type} {mc.access_token}",
                                    "Accept": "application/json"})
    except requests.RequestException as exc:
        raise NetworkError("无法连接 Minecraft 档案服务", detail=str(exc)) from exc

    if resp.status_code == 401:
        raise MinecraftAuthError("Minecraft 令牌无效或已过期", detail="HTTP 401")
    if resp.status_code == 404:
        # HMCL 在这里额外查一次授权，区分"没买游戏"和"档案不存在"
        if not has_license(mc, session):
            raise MinecraftAuthError("这个微软账号没有 Minecraft: Java 版的正版授权。")
        raise MinecraftAuthError("该账号没有 Minecraft 档案（可能玩家名还没设置）。")
    if resp.status_code >= 400:
        raise MinecraftAuthError(f"获取 Minecraft 档案失败（HTTP {resp.status_code}）",
                                 detail=resp.text[:200])
    data = resp.json()
    if not data.get("id") or not data.get("name"):
        raise MinecraftAuthError("Minecraft 档案响应缺少 id / name", detail=str(data)[:200])
    return Profile(uuid=data["id"], name=data["name"])


def has_license(mc: McToken, session: requests.Session) -> bool:
    """检查账号是否持有 Minecraft: Java 版（/entitlements/mcstore）。"""
    try:
        resp = session.get(ENTITLEMENTS_URL, timeout=30,
                           headers={"Authorization": f"{mc.token_type} {mc.access_token}",
                                    "Accept": "application/json"})
    except requests.RequestException as exc:
        raise NetworkError("无法连接 Minecraft 授权服务", detail=str(exc)) from exc
    if resp.status_code != 200:
        return False
    try:
        items = resp.json().get("items", [])
    except ValueError:
        return False
    return any(item.get("name") == "product_minecraft" for item in items) or bool(items)
