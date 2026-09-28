"""访问令牌校验（为「对外发布」预留的那道门）。

约定（刻意做得保守、可验证）：

* 令牌取自环境变量 **`ECAS_AUTH_TOKEN`**；**没设置 = fail-closed**：`/api/*` 一律 503、
  容器入口更是直接拒绝启动。只有显式设 `ECAS_ALLOW_NO_AUTH=1` 的本机开发才放行。
  （2026-09-28 安全审查 M-03 改的就是这条：原来「没设置 = 放行」，一旦容器里 secret
  没挂上——现场就发生过——就成了「以为有门、其实全开」。）
* 客户端三种带法都认，按优先级：
  1. `Authorization: Bearer <token>` —— 正常 HTTP 请求；
  2. 查询串 `?token=<token>` —— WebSocket / 直接下载（`<a href>` 带不了请求头）；
  3. Cookie `ecas_token` —— 浏览器刷新后仍带着。
* 比较用 `secrets.compare_digest`（定长、抗计时侧信道），不用 `==`。
* 被拒的请求记一条 warning（含来源），这样日志里能看出「有人在扫端口」。
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Mapping

from fastapi import HTTPException, Request, WebSocket, status

log = logging.getLogger("ecas.auth")

#: 浏览器侧保存令牌用的 Cookie 名（前端也会写同名 localStorage 项）
COOKIE_NAME = "ecas_token"

#: fail-closed 时返回给客户端的话（不泄内部细节，只指路）
NO_TOKEN_DETAIL = (
    "服务未配置访问令牌（ECAS_AUTH_TOKEN 为空）：已按 fail-closed 拒绝。"
    "本机开发请显式设 ECAS_ALLOW_NO_AUTH=1。"
)


def token_from(
    headers: Mapping[str, str],
    query_token: str | None = None,
    cookies: Mapping[str, str] | None = None,
) -> str | None:
    """从三种位置里取出客户端提供的令牌（取到第一个非空的）。"""
    auth = headers.get("authorization") or ""
    if auth[:7].lower() == "bearer ":
        value = auth[7:].strip()
        if value:
            return value
    if query_token:
        return query_token
    if cookies:
        value = cookies.get(COOKIE_NAME)
        if value:
            return value
    return None


def token_ok(provided: str | None, expected: str) -> bool:
    """配置了则要求常量时间相等；**没配置一律 False**（fail-closed，由调用方决定是否放行）。

    ⚠️ 必须按 **UTF-8 字节**比较：`secrets.compare_digest` 对含非 ASCII 字符的 `str`
    会抛 `TypeError: comparing strings with non-ASCII characters is not supported`，
    而令牌完全可能带中文/特殊符号。
    """
    if not expected or not provided:
        return False
    return secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def _client_of(scope_client: object) -> str:
    if isinstance(scope_client, (tuple, list)) and scope_client:
        return str(scope_client[0])
    return "?"


async def require_auth(request: Request) -> None:
    """FastAPI 依赖：套在 `/api/*` 上。

    没配令牌时**不会**放行（fail-closed）：返回 503 并明确说「服务没配令牌」——
    这样「secret 没挂上」会以一次响亮的失败暴露出来，而不是悄悄变成全开。
    例外只有显式声明的本机开发模式（`ECAS_ALLOW_NO_AUTH=1`）。
    """
    settings = request.app.state.settings
    expected: str = settings.auth_token
    if not expected:
        if settings.allow_no_auth:
            return
        log.error(
            "拒绝请求：服务未配置访问令牌（fail-closed）：%s %s（来源 %s）",
            request.method,
            request.url.path,
            _client_of(request.client),
        )
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=NO_TOKEN_DETAIL)
    provided = token_from(request.headers, request.query_params.get("token"), request.cookies)
    if token_ok(provided, expected):
        return
    log.warning(
        "拒绝未授权请求：%s %s（来源 %s，%s）",
        request.method,
        request.url.path,
        _client_of(request.client),
        "未带令牌" if not provided else "令牌错误",
    )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="缺少或错误的访问令牌",
        headers={"WWW-Authenticate": "Bearer"},
    )


def ws_authorized(websocket: WebSocket) -> bool:
    """WebSocket 握手前的校验（浏览器不能用 WS 请求头，所以查 `?token=` / Cookie）。

    没配令牌时同样 fail-closed（`False`），除非显式开了本机开发模式。
    """
    settings = websocket.app.state.settings
    expected: str = settings.auth_token
    if not expected:
        if settings.allow_no_auth:
            return True
        log.error("拒绝未授权 WebSocket：服务未配置访问令牌（fail-closed）：%s", websocket.url.path)
        return False
    provided = token_from(websocket.headers, websocket.query_params.get("token"), websocket.cookies)
    ok = token_ok(provided, expected)
    if not ok:
        log.warning(
            "拒绝未授权 WebSocket：%s（来源 %s）",
            websocket.url.path,
            _client_of(websocket.client),
        )
    return ok
