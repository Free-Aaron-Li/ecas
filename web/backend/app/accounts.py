"""网页账户：**PostgreSQL 为唯一权威存储**（内部 API，只给网关 Worker 调）。

依据：先生 2026-09-27 令「账户数据存入数据库」。此前账户记录只存在 Cloudflare KV
（键 `acct:<用户名>`），数据库里查不到、也不随库备份；本模块把它落到 `web_account` 表。

约定：

* **明文口令永不落库、永不进日志**。库里只有 PBKDF2-SHA256(iter=100000, salt=16B, 32B 输出)
  的 `salt` / `hash`（b64url 无填充）——与旧 Worker/CLI 的口径逐字节一致，
  所以从 KV 迁过来的记录（`POST /internal/accounts/import` 原样搬运）口令照旧可用。
* **哈希不出库**：列表/详情接口只返回 `username/role/disabled/must_change/se/时间戳`，
  `salt`/`hash` 一概不返回；校验口令走 `POST .../verify`（明文只在这一跳里出现）。
* **只有网关能调**：`/internal/accounts*` 只认 `Authorization: Bearer <ECAS_AUTH_TOKEN>`，
  且 `ECAS_AUTH_TOKEN` 为空时**直接 503 拒绝**（开发模式放行会让账户 CRUD 敞开）。
* `se`（会话世代）：改密 / 停用 +1，网关侧据此让旧 cookie 立即失效。

路由一览（全部挂在 `/internal/accounts`，网关 Worker 用服务绑定调）：

    GET    /internal/accounts                     列表（不含哈希）
    GET    /internal/accounts/{u}                 详情（不含哈希）
    POST   /internal/accounts                     新建（明文口令 → 哈希）
    POST   /internal/accounts/import              原样导入（带 salt/hash，用于从 KV 迁移）
    POST   /internal/accounts/{u}/verify          校验口令（登录用；顺带记 last_login_at）
    POST   /internal/accounts/{u}/password        改密 / 重置（可要求校验旧口令）
    PATCH  /internal/accounts/{u}                 停用 / 启用 / 强制改密 / 改角色
    DELETE /internal/accounts/{u}                 删除
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from . import db

log = logging.getLogger("ecas.accounts")

#: PBKDF2 参数（与 Worker/CLI 一致，改这三个值等于让所有旧口令失效）
ALGO = "pbkdf2-sha256"
ITER = 100_000
SALT_BYTES = 16
HASH_BYTES = 32
MIN_PW = 8

USER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")

DDL = (
    """
    CREATE TABLE IF NOT EXISTS web_account (
        username      text        PRIMARY KEY,
        role          text        NOT NULL DEFAULT 'user',
        disabled      boolean     NOT NULL DEFAULT false,
        must_change   boolean     NOT NULL DEFAULT false,
        algo          text        NOT NULL DEFAULT 'pbkdf2-sha256',
        iter          integer     NOT NULL DEFAULT 100000,
        salt          text        NOT NULL,
        hash          text        NOT NULL,
        se            integer     NOT NULL DEFAULT 0,
        created_at    timestamptz NOT NULL DEFAULT now(),
        updated_at    timestamptz NOT NULL DEFAULT now(),
        last_login_at timestamptz,
        CONSTRAINT web_account_role_chk CHECK (role IN ('admin', 'user'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS web_account_role_idx ON web_account (role)",
)


def ensure_schema(pool: ConnectionPool) -> None:
    """启动时建表（幂等）。失败只记日志，不让后端起不来。"""
    for statement in DDL:
        db.execute(pool, statement)


# ── 口令哈希（与旧 Worker/CLI 同口径）─────────────────────────────────────
def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, salt: str | None = None, iterations: int = ITER) -> dict[str, Any]:
    """算出 `{algo, iter, salt, hash}`（salt 不给就随机生成）。"""
    salt_b = unb64u(salt) if salt else secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_b, iterations, HASH_BYTES)
    return {"algo": ALGO, "iter": iterations, "salt": b64u(salt_b), "hash": b64u(digest)}


def password_ok(row: dict[str, Any], password: str) -> bool:
    """常数时间比对（`compare_digest` 按 ASCII 字节比，口令可能含中文）。"""
    if not row or not row.get("salt") or not row.get("hash"):
        return False
    try:
        candidate = hash_password(password, row["salt"], int(row.get("iter") or ITER))["hash"]
    except Exception:  # 记录被改坏（salt 非法 base64 等）⇒ 当作不匹配，别 500
        return False
    return hmac.compare_digest(candidate.encode(), str(row["hash"]).encode())


# ── 查询/写入 ─────────────────────────────────────────────────────────────
#: 对外（网关）能看到的字段：**没有 salt/hash**
PUBLIC_COLS = (
    "username, role, disabled, must_change, algo, iter, se, "
    "created_at, updated_at, last_login_at"
)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value)


def public(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """把库里的行整成网关要的形状（时间统一成 ISO-Z 字符串）。"""
    if not row:
        return None
    return {
        "username": row["username"],
        "role": row["role"],
        "disabled": bool(row["disabled"]),
        "must_change": bool(row["must_change"]),
        "algo": row.get("algo"),
        "iter": int(row.get("iter") or ITER),
        "se": int(row.get("se") or 0),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
        "last_login_at": _iso(row.get("last_login_at")),
    }


def get(pool: ConnectionPool, username: str) -> dict[str, Any] | None:
    """取**完整**记录（含 salt/hash）——只在校验口令/改密时用，不外传。"""
    return db.fetch_one(
        pool,
        "SELECT username, role, disabled, must_change, algo, iter, salt, hash, se, created_at, updated_at, last_login_at"
        "  FROM web_account WHERE username = %(u)s",
        {"u": username},
    )


def listing(pool: ConnectionPool) -> list[dict[str, Any]]:
    rows = db.fetch_all(pool, f"SELECT {PUBLIC_COLS} FROM web_account ORDER BY username")
    return [public(r) for r in rows]  # type: ignore[misc]


def normalize(username: str) -> str:
    return str(username or "").strip().lower()


def check_username(username: str) -> str:
    if not USER_RE.match(username):
        raise HTTPException(400, "用户名不合法：2–32 位小写字母/数字/._-，且须以字母或数字开头")
    return username


def check_password_strength(password: str) -> str:
    if len(password) < MIN_PW:
        raise HTTPException(400, f"口令至少 {MIN_PW} 个字符")
    return password


def create(
    pool: ConnectionPool,
    username: str,
    password: str,
    role: str = "user",
    must_change: bool = False,
    se: int = 1,
) -> dict[str, Any]:
    """新建账户（已存在则 409）。"""
    username = check_username(normalize(username))
    check_password_strength(password)
    if get(pool, username):
        raise HTTPException(409, f"账户 {username} 已存在")
    cred = hash_password(password)
    row = db.execute(
        pool,
        """
        INSERT INTO web_account (username, role, disabled, must_change, algo, iter, salt, hash, se)
        VALUES (%(u)s, %(role)s, false, %(must)s, %(algo)s, %(iter)s, %(salt)s, %(hash)s, %(se)s)
        RETURNING username
        """,
        {
            "u": username,
            "role": "admin" if role == "admin" else "user",
            "must": bool(must_change),
            "se": int(se),
            **cred,
        },
    )
    if not row:
        raise HTTPException(500, "写入账户失败")
    log.info("账户已创建：%s（角色 %s）", username, "admin" if role == "admin" else "user")
    return public(get(pool, username))  # type: ignore[return-value]


def import_records(pool: ConnectionPool, records: list[dict[str, Any]]) -> dict[str, Any]:
    """原样导入（salt/hash 照搬）——从 KV 迁移用，**不接触明文口令**。"""
    done: list[str] = []
    for rec in records:
        username = check_username(normalize(rec.get("username", "")))
        salt, digest = str(rec.get("salt") or ""), str(rec.get("hash") or "")
        if not salt or not digest:
            raise HTTPException(400, f"记录 {username} 缺少 salt/hash")
        db.execute(
            pool,
            """
            INSERT INTO web_account (username, role, disabled, must_change, algo, iter, salt, hash, se)
            VALUES (%(u)s, %(role)s, %(disabled)s, %(must)s, %(algo)s, %(iter)s, %(salt)s, %(hash)s, %(se)s)
            ON CONFLICT (username) DO UPDATE
               SET role = EXCLUDED.role, disabled = EXCLUDED.disabled, must_change = EXCLUDED.must_change,
                   algo = EXCLUDED.algo, iter = EXCLUDED.iter, salt = EXCLUDED.salt, hash = EXCLUDED.hash,
                   se = EXCLUDED.se, updated_at = now()
            """,
            {
                "u": username,
                "role": "admin" if rec.get("role") == "admin" else "user",
                "disabled": bool(rec.get("disabled")),
                "must": bool(rec.get("must_change")),
                "algo": str(rec.get("algo") or ALGO),
                "iter": int(rec.get("iter") or ITER),
                "salt": salt,
                "hash": digest,
                "se": int(rec.get("se") or 0),
            },
        )
        done.append(username)
    log.info("账户已导入：%s", ", ".join(done) or "（空）")
    return {"imported": done}


def verify(pool: ConnectionPool, username: str, password: str) -> dict[str, Any] | None:
    """校验口令；通过则记一次 last_login_at 并返回**公开**记录。"""
    username = normalize(username)
    row = get(pool, username)
    if not row or row.get("disabled") or not password_ok(row, password):
        return None
    db.execute(pool, "UPDATE web_account SET last_login_at = now() WHERE username = %(u)s", {"u": username})
    row["last_login_at"] = datetime.now(timezone.utc)
    return public(row)


def set_password(
    pool: ConnectionPool,
    username: str,
    new_password: str,
    old_password: str | None = None,
    require_old: bool = False,
    must_change: bool = False,
) -> dict[str, Any]:
    """改密 / 重置：`se` +1（旧 cookie 立即失效）。`require_old` 时旧口令必须对。"""
    username = check_username(normalize(username))
    row = get(pool, username)
    if not row:
        raise HTTPException(404, f"账户 {username} 不存在")
    if require_old or old_password is not None:
        if not old_password or not password_ok(row, old_password):
            raise HTTPException(401, "当前口令不正确")
    check_password_strength(new_password)
    cred = hash_password(new_password)
    db.execute(
        pool,
        """
        UPDATE web_account
           SET algo = %(algo)s, iter = %(iter)s, salt = %(salt)s, hash = %(hash)s,
               must_change = %(must)s, se = se + 1, updated_at = now()
         WHERE username = %(u)s
        """,
        {"u": username, "must": bool(must_change), **cred},
    )
    log.info("账户口令已更新：%s（会话世代 +1）", username)
    return public(get(pool, username))  # type: ignore[return-value]


def patch(pool: ConnectionPool, username: str, **fields: Any) -> dict[str, Any]:
    """停用/启用、强制改密、改角色。停用时 `se` +1（踢掉既有会话）。"""
    username = check_username(normalize(username))
    row = get(pool, username)
    if not row:
        raise HTTPException(404, f"账户 {username} 不存在")
    sets: list[str] = []
    params: dict[str, Any] = {"u": username}
    if "disabled" in fields and fields["disabled"] is not None:
        disabled = bool(fields["disabled"])
        sets.append("disabled = %(disabled)s")
        params["disabled"] = disabled
        if disabled:
            sets.append("se = se + 1")
    if "must_change" in fields and fields["must_change"] is not None:
        sets.append("must_change = %(must)s")
        params["must"] = bool(fields["must_change"])
    if "role" in fields and fields["role"] is not None:
        role = "admin" if fields["role"] == "admin" else "user"
        sets.append("role = %(role)s")
        params["role"] = role
    if not sets:
        raise HTTPException(400, "没有要改的字段")
    sets.append("updated_at = now()")
    db.execute(pool, f"UPDATE web_account SET {', '.join(sets)} WHERE username = %(u)s", params)
    log.info("账户已更新：%s（%s）", username, ", ".join(s.split(" =")[0] for s in sets[:-1]))
    return public(get(pool, username))  # type: ignore[return-value]


def delete(pool: ConnectionPool, username: str) -> None:
    username = check_username(normalize(username))
    if not get(pool, username):
        raise HTTPException(404, f"账户 {username} 不存在")
    db.execute(pool, "DELETE FROM web_account WHERE username = %(u)s", {"u": username})
    log.info("账户已删除：%s", username)


# ── 内部 API（只有网关 Worker 能调）───────────────────────────────────────
def require_internal_token(request: Request) -> None:
    """内部接口的闸门：**只认请求头 Bearer**，且令牌未配置时拒绝（不放行）。

    与 `app.auth.require_auth` 的区别：那个是给 `/api/*` 的对外闸门，允许 `?token=` 与 Cookie
    （浏览器下载/WS 用）；账户 CRUD 不外发，所以这里更严——查询串/ Cookie 一律不认。
    """
    expected: str = request.app.state.settings.auth_token
    if not expected:
        raise HTTPException(503, "内部接口未启用：ECAS_AUTH_TOKEN 未设置")
    provided = request.headers.get("authorization") or ""
    token = provided[7:].strip() if provided[:7].lower() == "bearer " else ""
    if not token or not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
        log.warning("拒绝内部账户接口调用：%s %s", request.method, request.url.path)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少或错误的内部令牌")


class CreateBody(BaseModel):
    username: str
    password: str
    role: str = "user"
    must_change: bool = False
    se: int = 1


class ImportBody(BaseModel):
    records: list[dict[str, Any]] = Field(default_factory=list)


class VerifyBody(BaseModel):
    password: str


class PasswordBody(BaseModel):
    new_password: str
    old_password: str | None = None
    require_old: bool = False
    must_change: bool = False


class PatchBody(BaseModel):
    disabled: bool | None = None
    must_change: bool | None = None
    role: str | None = None


router = APIRouter(prefix="/internal/accounts", tags=["accounts"])
#: 所有账户路由都过同一道闸门（见 `require_internal_token`）
GUARD = [Depends(require_internal_token)]


def _pool(request: Request) -> ConnectionPool:
    return request.app.state.pool


@router.get("", dependencies=GUARD)
def api_list(request: Request) -> dict[str, Any]:
    return {"accounts": listing(_pool(request))}


@router.post("", dependencies=GUARD, status_code=201)
def api_create(request: Request, body: CreateBody) -> dict[str, Any]:
    account = create(
        _pool(request),
        body.username,
        body.password,
        role=body.role,
        must_change=body.must_change,
        se=body.se,
    )
    return {"account": account}


@router.post("/import", dependencies=GUARD)
def api_import(request: Request, body: ImportBody) -> dict[str, Any]:
    return import_records(_pool(request), body.records)


@router.get("/{username}", dependencies=GUARD)
def api_get(request: Request, username: str) -> dict[str, Any]:
    row = get(_pool(request), normalize(username))
    if not row:
        raise HTTPException(404, f"账户 {username} 不存在")
    return {"account": public(row)}


@router.post("/{username}/verify", dependencies=GUARD)
def api_verify(request: Request, username: str, body: VerifyBody) -> dict[str, Any]:
    account = verify(_pool(request), username, body.password)
    if not account:
        raise HTTPException(401, "用户名或口令不正确")
    return {"ok": True, "account": account}


@router.post("/{username}/password", dependencies=GUARD)
def api_password(request: Request, username: str, body: PasswordBody) -> dict[str, Any]:
    account = set_password(
        _pool(request),
        username,
        body.new_password,
        old_password=body.old_password,
        require_old=body.require_old,
        must_change=body.must_change,
    )
    return {"account": account}


@router.patch("/{username}", dependencies=GUARD)
def api_patch(request: Request, username: str, body: PatchBody) -> dict[str, Any]:
    return {"account": patch(_pool(request), username, **body.model_dump())}


@router.delete("/{username}", dependencies=GUARD)
def api_delete(request: Request, username: str) -> dict[str, Any]:
    delete(_pool(request), username)
    return {"deleted": normalize(username)}
