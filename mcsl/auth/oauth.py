"""微软 OAuth：设备码流 + 浏览器授权码流 + refresh 续期。

端点和参数对齐 HMCL（HMCLCore/src/main/java/org/jackhuang/hmcl/auth/OAuth.java）：
    scope = "XboxLive.signin offline_access"
    * 设备码：POST {device_code_url}  form: client_id, scope
              POST {token_url}        form: grant_type=urn:ietf:params:oauth:grant-type:device_code,
                                            device_code/code, client_id
    * 授权码：GET  {authorize_url}   client_id, response_type=code, redirect_uri, scope,
                                    prompt=select_account, code_challenge, code_challenge_method=S256, state
              POST {token_url}        client_id, code, grant_type=authorization_code,
                                    code_verifier, redirect_uri, scope
    * 续期：  POST {token_url}        client_id, refresh_token, grant_type=refresh_token

两套端点预设：
    live —— login.live.com（官方启动器的公开 client_id 00000000402b5328 实测可用）
    v2   —— login.microsoftonline.com/consumers/oauth2/v2.0（用你自己注册的 Azure 应用）
"""
from __future__ import annotations

import base64
import hashlib
import html
import logging
import secrets
import socket
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable

import requests

from .errors import NetworkError, OAuthCancelled, OAuthError

log = logging.getLogger(__name__)

SCOPE = "XboxLive.signin offline_access"

# 官方启动器使用的公开 client id。它配 Live Connect 端点（live 预设）实测能拿到
# device code；在 microsoftonline 的 v2 devicecode 端点上会被拒（AADSTS700016）。
# 想要完全自主，就注册自己的 Azure 应用并填进配置。
DEFAULT_CLIENT_ID = "00000000402b5328"

REDIRECT_PORT_CANDIDATES = (29111, 29112, 29113, 29114, 29115)
REQUEST_TIMEOUT = 30
MAX_WAIT_SECONDS = 900  # 设备码最多等 15 分钟（HMCL 也是这个上限）


@dataclass(frozen=True)
class OAuthEndpoints:
    authorize: str
    token: str
    device_code: str
    # 设备码请求的额外参数：Live Connect 端点强制要求 response_type=device_code
    device_code_extra: dict[str, str] = field(default_factory=dict)


PRESETS: dict[str, OAuthEndpoints] = {
    "live": OAuthEndpoints(
        authorize="https://login.live.com/oauth20_authorize.srf",
        token="https://login.live.com/oauth20_token.srf",
        device_code="https://login.live.com/oauth20_connect.srf",
        device_code_extra={"response_type": "device_code"},
    ),
    "v2": OAuthEndpoints(
        authorize="https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize",
        token="https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
        device_code="https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode",
    ),
}


def get_endpoints(preset: str) -> OAuthEndpoints:
    try:
        return PRESETS[preset]
    except KeyError:
        log.warning("未知端点预设 %r，回退到 live", preset)
        return PRESETS["live"]


@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    expires_in: int = 3600


@dataclass
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    interval: int
    expires_in: int


class CancelToken:
    """可被 UI 线程取消的令牌。"""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float) -> bool:  # True = 已被取消
        return self._event.wait(timeout)


def _raise_for_oauth_error(payload: dict, status: int) -> None:
    err = payload.get("error")
    if err:
        desc = payload.get("error_description") or ""
        raise OAuthError(str(err), str(desc))
    if status >= 400:
        raise OAuthError(f"http_{status}", f"HTTP {status}: {str(payload)[:200]}")


def _post_form(session: requests.Session, url: str, data: dict[str, str]) -> tuple[int, dict]:
    try:
        resp = session.post(url, data=data, timeout=REQUEST_TIMEOUT,
                            headers={"Accept": "application/json"})
    except requests.RequestException as exc:
        raise NetworkError("无法连接微软登录服务器", detail=str(exc)) from exc
    try:
        payload = resp.json()
    except ValueError:
        payload = {"raw": resp.text[:300]}
    return resp.status_code, payload


# --------------------------------------------------------------------------
# 设备码流
# --------------------------------------------------------------------------
def request_device_code(session: requests.Session, client_id: str,
                        endpoints: OAuthEndpoints, scope: str = SCOPE) -> DeviceCode:
    form = {"client_id": client_id, "scope": scope}
    form.update(endpoints.device_code_extra)
    status, payload = _post_form(session, endpoints.device_code, form)
    _raise_for_oauth_error(payload, status)
    for key in ("device_code", "user_code", "verification_uri"):
        if not payload.get(key):
            raise OAuthError("malformed_response", f"设备码响应缺少字段 {key}")
    return DeviceCode(
        device_code=payload["device_code"],
        user_code=payload["user_code"],
        verification_uri=payload.get("verification_uri") or payload.get("verification_url", ""),
        interval=int(payload.get("interval") or 5),
        expires_in=int(payload.get("expires_in") or MAX_WAIT_SECONDS),
    )


def poll_device_code(session: requests.Session, client_id: str, endpoints: OAuthEndpoints,
                     info: DeviceCode, *, cancel: CancelToken | None = None,
                     on_tick: Callable[[float], None] | None = None,
                     scope: str = SCOPE) -> Tokens:
    """轮询直到用户在浏览器里完成登录。"""
    interval = max(info.interval, 1)
    deadline = time.monotonic() + min(info.expires_in, MAX_WAIT_SECONDS)
    while True:
        if cancel is not None and cancel.wait(interval):
            raise OAuthCancelled("已取消设备码登录")
        if time.monotonic() >= deadline:
            raise OAuthCancelled("设备码已过期，请重新发起登录")
        if on_tick is not None:
            on_tick(max(0.0, deadline - time.monotonic()))
        # HMCL 用的是 "code"，RFC 8628 规定是 "device_code"，两个都发以兼容两套端点。
        status, payload = _post_form(session, endpoints.token, {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": info.device_code,
            "code": info.device_code,
            "client_id": client_id,
        })
        err = payload.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        if err == "expired_token":
            raise OAuthCancelled("设备码已过期，请重新发起登录")
        if err in ("authorization_declined", "access_denied"):
            raise OAuthCancelled("你拒绝了本次授权")
        _raise_for_oauth_error(payload, status)
        if not payload.get("access_token"):
            raise OAuthError("malformed_response", "令牌响应中没有 access_token")
        return Tokens(payload["access_token"], payload.get("refresh_token", ""),
                      int(payload.get("expires_in") or 3600))


# --------------------------------------------------------------------------
# 授权码流（本地回环 + PKCE）
# --------------------------------------------------------------------------
class _CallbackHandler(BaseHTTPRequestHandler):
    server_version = "MCSRLauncher/1.0"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 接口
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        srv: "_CallbackServer" = self.server  # type: ignore[assignment]
        if "code" in params and parsed.path == urllib.parse.urlparse(srv.redirect_uri).path:
            srv.code = params["code"][0]
            srv.state = (params.get("state") or [""])[0]
            body = _callback_page("登录成功", "可以关闭此页面，回到 MCSR-Launcher 继续。", ok=True)
            self._respond(body)
            srv.done.set()
        elif "error" in params:
            srv.error = (params.get("error_description") or params["error"])[0]
            body = _callback_page("登录失败", html.escape(srv.error), ok=False)
            self._respond(body)
            srv.done.set()
        else:
            # 浏览器可能请求 favicon 之类，忽略
            self.send_response(204)
            self.end_headers()

    def _respond(self, body: str) -> None:
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt: str, *args) -> None:  # 静音 http.server 的默认输出
        log.debug("oauth callback: " + fmt, *args)


def _callback_page(title: str, message: str, *, ok: bool) -> str:
    color = "#2e7d32" if ok else "#c62828"
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>MCSR-Launcher</title></head>
<body style="font-family:system-ui,'Microsoft YaHei',sans-serif;background:#111;color:#eee;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
<div style="text-align:center;padding:32px 40px;border:1px solid #333;border-radius:12px">
<h1 style="color:{color};font-size:20px;margin:0 0 12px">{html.escape(title)}</h1>
<p style="margin:0;color:#bbb">{message}</p></div></body></html>"""


class _CallbackServer(HTTPServer):
    code: str | None = None
    state: str = ""
    error: str = ""
    done: threading.Event
    redirect_uri: str

    def __init__(self, redirect_uri: str) -> None:
        parsed = urllib.parse.urlparse(redirect_uri)
        super().__init__((parsed.hostname or "127.0.0.1", parsed.port or 80), _CallbackHandler)
        self.redirect_uri = redirect_uri
        self.done = threading.Event()
        self.timeout = 1


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:128]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=")
    return verifier, challenge.decode("ascii")


def authorization_code_flow(session: requests.Session, client_id: str, endpoints: OAuthEndpoints,
                            *, cancel: CancelToken | None = None,
                            on_url: Callable[[str], None] | None = None,
                            open_browser: bool = True, scope: str = SCOPE) -> Tokens:
    """开本地端口接收回调，打开浏览器完成登录。

    注意：client_id 必须在该应用的注册里配好对应的 redirect_uri，否则微软会拒绝。
    """
    server: _CallbackServer | None = None
    last_error: Exception | None = None
    for port in REDIRECT_PORT_CANDIDATES:
        try:
            server = _CallbackServer(f"http://localhost:{port}/auth/callback")
            break
        except OSError as exc:  # 端口占用，换下一个
            last_error = exc
    if server is None:
        raise NetworkError("本地回调端口 29111-29115 全部被占用", detail=str(last_error))

    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)
    query = urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": server.redirect_uri,
        "scope": scope,
        "prompt": "select_account",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    })
    url = f"{endpoints.authorize}?{query}"
    if on_url is not None:
        on_url(url)
    if open_browser:
        threading.Thread(target=_safe_open, args=(url,), daemon=True).start()

    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True)
    thread.start()
    try:
        while not server.done.wait(0.5):
            if cancel is not None and cancel.cancelled:
                raise OAuthCancelled("已取消浏览器登录")
        if server.error:
            raise OAuthError("callback_error", server.error)
        if server.state and server.state != state:
            raise OAuthError("state_mismatch", "回调 state 不匹配，出于安全考虑已中止")
        if not server.code:
            raise OAuthError("no_code", "未收到授权码")
    finally:
        server.shutdown()
        server.server_close()

    status, payload = _post_form(session, endpoints.token, {
        "client_id": client_id,
        "code": server.code,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
        "redirect_uri": server.redirect_uri,
        "scope": scope,
    })
    _raise_for_oauth_error(payload, status)
    if not payload.get("access_token"):
        raise OAuthError("malformed_response", "令牌响应中没有 access_token")
    return Tokens(payload["access_token"], payload.get("refresh_token", ""),
                  int(payload.get("expires_in") or 3600))


def _safe_open(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception as exc:  # noqa: BLE001 - 打不开浏览器不该炸掉登录流程
        log.warning("打开浏览器失败：%s", exc)


# --------------------------------------------------------------------------
# 续期
# --------------------------------------------------------------------------
def refresh(session: requests.Session, client_id: str, endpoints: OAuthEndpoints,
            refresh_token: str, scope: str = SCOPE) -> Tokens:
    """用 refresh token 换新的 MSA access token。

    refresh token 与签发它的 client_id 绑定：换了 client_id 就必须重新登录
    （invalid_grant / AADSTS70000 会体现出来）。
    """
    status, payload = _post_form(session, endpoints.token, {
        "client_id": client_id,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
        "scope": scope,
    })
    if payload.get("error") == "invalid_grant":
        desc = str(payload.get("error_description") or "")
        if "AADSTS70000" in desc or "expired" in desc.lower() or "invalid" in desc.lower():
            raise OAuthCancelled("登录凭据已失效，需要重新登录微软账号")
    _raise_for_oauth_error(payload, status)
    if not payload.get("access_token"):
        raise OAuthError("malformed_response", "刷新响应中没有 access_token")
    return Tokens(payload["access_token"], payload.get("refresh_token") or refresh_token,
                  int(payload.get("expires_in") or 3600))
