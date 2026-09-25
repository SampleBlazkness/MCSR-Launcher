"""Xbox Live（XBL）+ XSTS 认证。参数与 HMCL 的 MicrosoftService 一致。"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

from .errors import NetworkError, XboxError

log = logging.getLogger(__name__)

XBL_AUTH_URL = "https://user.auth.xboxlive.com/user/authenticate"
XSTS_AUTH_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"
XSTS_RELYING_PARTY = "rp://api.minecraftservices.com/"

# XSTS 的 XErr 错误码 → 人话
XERR_MESSAGES: dict[int, str] = {
    2148916227: "该微软账号已被 Xbox Live 封禁。",
    2148916229: "该微软账号是儿童账号，且未加入任何家庭组，请先用成人账号把它加进家庭组。",
    2148916233: "该微软账号还没有 Xbox 档案。请先到 xbox.com 创建一个档案（免费），再回来登录。",
    2148916235: "该账号所在地区不支持 Xbox Live，需要把账号地区改成支持的地区。",
    2148916236: "该账号需要在 Xbox 网页端完成年龄验证（韩国地区要求）。",
    2148916237: "该账号需要在 Xbox 网页端完成年龄验证（韩国地区要求）。",
    2148916238: "该账号未满 18 岁，必须由成人账号把它加入家庭组才能登录。",
    2148916250: "该账号被限制登录 Xbox Live。",
    2148916252: "该账号的 Xbox 档案被锁定。",
}


@dataclass
class XblToken:
    token: str
    uhs: str


@dataclass
class XstsToken:
    token: str
    uhs: str
    xuid: str = ""


def _post_json(url: str, payload: dict, session: requests.Session, *,
               allow_401: bool = False) -> tuple[int, dict, str]:
    try:
        resp = session.post(url, json=payload, timeout=30,
                            headers={"Accept": "application/json",
                                     "x-xbl-contract-version": "3"})
    except requests.RequestException as exc:
        raise NetworkError("无法连接 Xbox Live 服务器", detail=str(exc)) from exc
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code >= 400 and not (allow_401 and resp.status_code == 401):
        if resp.status_code == 400:
            raise XboxError("Xbox Live 拒绝了请求（HTTP 400），通常是账号或令牌状态异常。",
                            detail=f"HTTP 400: {str(data)[:200]}")
        raise XboxError(f"Xbox Live 返回 HTTP {resp.status_code}",
                        detail=str(data)[:200])
    return resp.status_code, data, resp.text


def _extract(data: dict) -> tuple[str, str, str]:
    token = data.get("Token")
    claims = (data.get("DisplayClaims") or {}).get("xui") or []
    if not token or not claims or "uhs" not in claims[0]:
        raise XboxError("Xbox Live 返回了无法识别的响应", detail=str(data)[:200])
    first = claims[0]
    return token, str(first["uhs"]), str(first.get("xid", ""))


def xbl_authenticate(msa_access_token: str, session: requests.Session) -> XblToken:
    """用 MSA access token 换取 Xbox Live 令牌。"""
    _, data, _ = _post_json(XBL_AUTH_URL, {
        "Properties": {
            "AuthMethod": "RPS",
            "SiteName": "user.auth.xboxlive.com",
            "RpsTicket": "d=" + msa_access_token,
        },
        "RelyingParty": "http://auth.xboxlive.com",
        "TokenType": "JWT",
    }, session)
    token, uhs, _ = _extract(data)
    log.debug("XBL 认证成功（uhs=%s…）", uhs[:6])
    return XblToken(token, uhs)


def xsts_authorize(xbl: XblToken, session: requests.Session) -> XstsToken:
    """用 XBL 令牌换 XSTS 令牌（RelyingParty 指向 Minecraft Services）。"""
    status, data, _ = _post_json(XSTS_AUTH_URL, {
        "Properties": {"SandboxId": "RETAIL", "UserTokens": [xbl.token]},
        "RelyingParty": XSTS_RELYING_PARTY,
        "TokenType": "JWT",
    }, session, allow_401=True)

    if status == 401 or (isinstance(data, dict) and data.get("XErr")):
        xerr = int(data.get("XErr") or 0)
        redirect = str(data.get("Redirect") or "")
        message = XERR_MESSAGES.get(xerr)
        if message is None:
            message = f"Xbox Live 授权被拒（XErr={xerr}）。"
        if redirect:
            message += f" 参考页面：{redirect}"
        raise XboxError(message, xerr=xerr, redirect=redirect)

    token, uhs, xuid = _extract(data)
    log.debug("XSTS 认证成功（xuid=%s）", xuid or "未知")
    return XstsToken(token, uhs, xuid)
