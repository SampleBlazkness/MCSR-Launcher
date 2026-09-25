"""账号管理器：把 OAuth → XBL → XSTS → Minecraft 串起来，并缓存会话。

对外主入口：
    mgr.login(...)            走设备码或浏览器登录，成功后落盘
    mgr.acquire_session(...)  拿到可用的 Minecraft 令牌（必要时自动续期）
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

import requests

from ..config import AuthConfig
from . import minecraft, oauth, xbox
from .errors import AuthError, OAuthCancelled
from .store import Account, AccountStore, StoreData

log = logging.getLogger(__name__)

# Minecraft 令牌提前多少秒算过期（留出启动流程的余量）
TOKEN_SKEW = 120

StatusCb = Callable[[str], None]
DeviceCodeCb = Callable[[oauth.DeviceCode], None]


@dataclass
class MCSession:
    uuid: str
    name: str
    access_token: str
    token_type: str
    client_id: str
    xuid: str = ""
    expires_at: float = 0.0
    refresh_token: str = ""

    @property
    def valid(self) -> bool:
        return bool(self.access_token) and time.time() + TOKEN_SKEW < self.expires_at


class AccountManager:
    def __init__(self, auth_cfg: AuthConfig, store: AccountStore | None = None) -> None:
        self.cfg = auth_cfg
        self.store = store or AccountStore()
        self.http = requests.Session()
        self.http.headers["User-Agent"] = "MCSR-Launcher/0.1 (+https://github.com/SampleBlazkness)"
        self._session: MCSession | None = None

    # ------------------------------------------------------------ 账号列表
    @property
    def data(self) -> StoreData:
        return self.store.load()

    @property
    def accounts(self) -> list[Account]:
        return self.data.accounts

    @property
    def selected(self) -> Account | None:
        data = self.data
        for acc in data.accounts:
            if acc.uuid == data.selected:
                return acc
        return data.accounts[0] if data.accounts else None

    def select(self, uuid: str) -> None:
        data = self.data
        if any(a.uuid == uuid for a in data.accounts):
            data.selected = uuid
            self.store.save(data)
            self._session = None

    def logout(self, uuid: str) -> None:
        self.store.remove(uuid)
        self._session = None

    # ------------------------------------------------------------ 登录
    def login(self, *, flow: str | None = None, client_id: str | None = None,
              preset: str | None = None, on_status: StatusCb | None = None,
              on_device_code: DeviceCodeCb | None = None,
              cancel: oauth.CancelToken | None = None, open_browser: bool = True) -> Account:
        """登录并保存账号。flow: device_code / auth_code。"""
        flow = flow or self.cfg.flow
        client_id = (client_id or self.cfg.client_id).strip()
        preset = preset or self.cfg.endpoint_preset
        if not client_id:
            raise AuthError("没有配置 client_id，请先在设置里填写微软应用 client id")
        endpoints = oauth.get_endpoints(preset)

        def status(msg: str) -> None:
            log.info(msg)
            if on_status:
                on_status(msg)

        status(f"正在向微软请求授权（{flow}，端点 {preset}）…")
        if flow == "auth_code":
            tokens = oauth.authorization_code_flow(
                self.http, client_id, endpoints, cancel=cancel,
                open_browser=open_browser,
                on_url=lambda url: status("已打开浏览器，请在浏览器里完成登录（如果没弹出请手动访问授权链接）"))
        else:
            info = oauth.request_device_code(self.http, client_id, endpoints)
            if on_device_code:
                on_device_code(info)
            status(f"请在浏览器中打开 {info.verification_uri} 并输入代码 {info.user_code}")
            tokens = oauth.poll_device_code(self.http, client_id, endpoints, info, cancel=cancel)

        status("微软授权成功，正在登录 Xbox Live 与 Minecraft…")
        account = self._build_account(tokens, client_id, preset, status)
        self.store.upsert(account)
        status(f"登录完成：{account.name}")
        return account

    def _build_account(self, tokens: oauth.Tokens, client_id: str, preset: str,
                       status: StatusCb) -> Account:
        """MSA 令牌 → XBL → XSTS → MC 令牌 → 档案，组装成 Account。"""
        xbl = xbox.xbl_authenticate(tokens.access_token, self.http)
        xsts = xbox.xsts_authorize(xbl, self.http)
        mc = minecraft.login_with_xbox(xsts, self.http)
        profile = minecraft.fetch_profile(mc, self.http)
        status(f"已登录为 {profile.name}")
        session = {
            "access_token": mc.access_token,
            "token_type": mc.token_type,
            "xuid": xsts.xuid,
            "expires_at": mc.expires_at,
        }
        return Account(
            uuid=profile.uuid,
            name=profile.name,
            refresh_token=tokens.refresh_token,
            client_id=client_id,
            endpoint_preset=preset,
            session=session,
        )

    # ------------------------------------------------------------ 取得会话
    def acquire_session(self, account: Account | None = None, *, force: bool = False,
                        on_status: StatusCb | None = None) -> MCSession:
        """返回可用的 Minecraft 会话；失效则自动刷新（用户无感）。"""
        account = account or self.selected
        if account is None:
            raise AuthError("还没有登录任何微软账号")

        def status(msg: str) -> None:
            log.info(msg)
            if on_status:
                on_status(msg)

        if not force and self._session is not None \
                and self._session.uuid == account.uuid and self._session.valid:
            return self._session

        # 1) 缓存的 MC 令牌还有效就直接用（省掉 4 次网络往返）
        if not force and account.session:
            cached = MCSession(
                uuid=account.uuid, name=account.name,
                access_token=account.session.get("access_token", ""),
                token_type=account.session.get("token_type", "Bearer"),
                client_id=account.client_id, xuid=account.session.get("xuid", ""),
                expires_at=float(account.session.get("expires_at") or 0),
                refresh_token=account.refresh_token,
            )
            if cached.valid:
                log.debug("复用缓存的 Minecraft 令牌（%d 秒后过期）",
                          int(cached.expires_at - time.time()))
                self._session = cached
                return cached

        # 2) 刷新 / 重新走认证链
        if not account.refresh_token:
            raise AuthError("该账号没有保存的登录凭据，请重新登录")
        if account.client_id and account.client_id != self.cfg.client_id.strip():
            raise AuthError(
                f"账号 {account.name} 是用 client_id {account.client_id} 登录的，"
                f"当前配置是 {self.cfg.client_id}。refresh token 与 client_id 绑定，"
                "请切换回原 client_id 或重新登录。")

        endpoints = oauth.get_endpoints(account.endpoint_preset or self.cfg.endpoint_preset)
        client_id = account.client_id or self.cfg.client_id.strip()
        status("正在刷新微软令牌…")
        tokens = oauth.refresh(self.http, client_id, endpoints, account.refresh_token)

        status("正在重新获取 Minecraft 令牌…")
        xbl = xbox.xbl_authenticate(tokens.access_token, self.http)
        xsts = xbox.xsts_authorize(xbl, self.http)
        mc = minecraft.login_with_xbox(xsts, self.http)
        profile = minecraft.fetch_profile(mc, self.http)

        session = MCSession(
            uuid=profile.uuid or account.uuid, name=profile.name,
            access_token=mc.access_token, token_type=mc.token_type,
            client_id=client_id, xuid=xsts.xuid, expires_at=mc.expires_at,
            refresh_token=tokens.refresh_token,
        )
        account.name = session.name
        account.uuid = session.uuid
        account.refresh_token = tokens.refresh_token
        account.session = {"access_token": mc.access_token, "token_type": mc.token_type,
                           "xuid": xsts.xuid, "expires_at": mc.expires_at}
        self.store.upsert(account)
        self._session = session
        status(f"Minecraft 令牌就绪（{profile.name}）")
        return session


__all__ = ["AccountManager", "MCSession", "Account", "AccountStore",
           "AuthError", "OAuthCancelled", "oauth", "xbox", "minecraft"]
