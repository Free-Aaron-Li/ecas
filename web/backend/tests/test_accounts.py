"""账户模块测试（打真实本地 PostgreSQL，与 test_alarms/test_api 同一套路）。

重点保三件事：

1. **哈希口径与旧 Worker/CLI 一致** —— 从 KV 迁过来的记录必须能继续用原口令登录；
2. **哈希不外传** —— 内部接口列表里不能出现 `salt`/`hash`；
3. **`se` 会话世代** —— 改密/停用 +1，网关据此让旧 cookie 立刻失效。
"""

from __future__ import annotations

import base64
import hashlib
import uuid

import pytest
from fastapi.testclient import TestClient

from app import accounts

TOKEN = "internal-token-for-test"
PW = "correct horse battery staple"


@pytest.fixture()
def client() -> TestClient:
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def auth(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setattr(client.app.state.settings, "auth_token", TOKEN)
    return {"authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def name() -> str:
    """每个用例一个独立用户名，跑完删掉（库里不留试验品）。"""
    return "zz" + uuid.uuid4().hex[:10]


def _cleanup(client: TestClient, *usernames: str) -> None:
    for u in usernames:
        try:
            accounts.delete(client.app.state.pool, u)
        except Exception:
            pass


# ── 纯函数：哈希口径 ──────────────────────────────────────────────────────
def test_哈希口径与旧实现逐字节一致() -> None:
    """旧 CLI 的 make_record：b64url(16B salt) 22 字符、b64url(32B) 43 字符、可复算。"""
    cred = accounts.hash_password(PW)
    assert cred["algo"] == "pbkdf2-sha256"
    assert cred["iter"] == accounts.ITER
    assert len(cred["salt"]) == 22
    assert len(cred["hash"]) == 43
    # 手工复算（不复用被测函数）—— 这条挂了说明口径变了，旧口令会全部失效
    salt = base64.urlsafe_b64decode(cred["salt"] + "=" * (-len(cred["salt"]) % 4))
    manual = base64.urlsafe_b64encode(
        hashlib.pbkdf2_hmac("sha256", PW.encode(), salt, accounts.ITER, 32)
    ).decode().rstrip("=")
    assert manual == cred["hash"]
    assert accounts.password_ok({"salt": cred["salt"], "hash": cred["hash"], "iter": accounts.ITER}, PW)
    assert not accounts.password_ok({"salt": cred["salt"], "hash": cred["hash"], "iter": accounts.ITER}, PW + "x")


def test_记录被改坏时不炸() -> None:
    assert not accounts.password_ok({"salt": "!!!不是 base64!!!", "hash": "x", "iter": 100000}, PW)
    assert not accounts.password_ok({}, PW)


# ── API：闸门 ─────────────────────────────────────────────────────────────
def test_不带令牌一律拒绝(client: TestClient, auth: dict[str, str], name: str) -> None:
    assert client.get("/internal/accounts").status_code == 401
    assert client.get("/internal/accounts", headers={"authorization": "Bearer wrong-token"}).status_code == 401
    # 查询串里的令牌**不认**（内部接口比 /api/* 更严）
    assert client.get(f"/internal/accounts?token={TOKEN}").status_code == 401
    assert client.get("/internal/accounts", headers=auth).status_code == 200


def test_令牌没配置时拒绝而不是放行(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client.app.state.settings, "auth_token", "")
    assert client.get("/internal/accounts").status_code == 503


# ── API：增删改查 ─────────────────────────────────────────────────────────
def test_创建校验改密停用删除全链路(client: TestClient, auth: dict[str, str], name: str) -> None:
    try:
        # 创建
        r = client.post("/internal/accounts", headers=auth, json={"username": name, "password": PW, "role": "user"})
        assert r.status_code == 201, r.text
        acc = r.json()["account"]
        assert acc["username"] == name and acc["role"] == "user" and acc["se"] == 1

        # 重复创建 → 409
        assert client.post(
            "/internal/accounts", headers=auth, json={"username": name, "password": PW}
        ).status_code == 409
        # 弱口令 / 非法用户名 → 400
        assert client.post(
            "/internal/accounts", headers=auth, json={"username": "zz" + uuid.uuid4().hex[:8], "password": "short"}
        ).status_code == 400
        assert client.post(
            "/internal/accounts", headers=auth, json={"username": "非法 名", "password": PW}
        ).status_code == 400

        # 列表：**不能带哈希**
        rows = client.get("/internal/accounts", headers=auth).json()["accounts"]
        mine = [a for a in rows if a["username"] == name]
        assert len(mine) == 1
        assert "hash" not in mine[0] and "salt" not in mine[0]
        assert "hash" not in client.get(f"/internal/accounts/{name}", headers=auth).text

        # 校验口令
        assert client.post(f"/internal/accounts/{name}/verify", headers=auth, json={"password": PW}).status_code == 200
        assert client.post(
            f"/internal/accounts/{name}/verify", headers=auth, json={"password": PW + "x"}
        ).status_code == 401

        # 改密：旧口令必须对；se+1
        assert client.post(
            f"/internal/accounts/{name}/password", headers=auth,
            json={"old_password": "错", "new_password": "another-good-pw", "require_old": True},
        ).status_code == 401
        r = client.post(
            f"/internal/accounts/{name}/password", headers=auth,
            json={"old_password": PW, "new_password": "another-good-pw", "require_old": True},
        )
        assert r.status_code == 200 and r.json()["account"]["se"] == 2
        assert client.post(
            f"/internal/accounts/{name}/verify", headers=auth, json={"password": PW}
        ).status_code == 401
        assert client.post(
            f"/internal/accounts/{name}/verify", headers=auth, json={"password": "another-good-pw"}
        ).status_code == 200

        # 停用：se 再 +1，且校验一律失败
        r = client.patch(f"/internal/accounts/{name}", headers=auth, json={"disabled": True})
        assert r.status_code == 200 and r.json()["account"]["se"] == 3
        assert client.post(
            f"/internal/accounts/{name}/verify", headers=auth, json={"password": "another-good-pw"}
        ).status_code == 401

        # 删除
        assert client.delete(f"/internal/accounts/{name}", headers=auth).status_code == 200
        assert client.get(f"/internal/accounts/{name}", headers=auth).status_code == 404
    finally:
        _cleanup(client, name)


def test_原样导入的旧记录能继续用原口令登录(client: TestClient, auth: dict[str, str], name: str) -> None:
    """模拟从 KV 迁移：salt/hash 照搬（不接触明文），验证口令仍然通过。"""
    cred = accounts.hash_password(PW)
    try:
        r = client.post(
            "/internal/accounts/import",
            headers=auth,
            json={"records": [{
                "username": name, "role": "admin", "must_change": False, "disabled": False,
                "algo": cred["algo"], "iter": cred["iter"], "salt": cred["salt"], "hash": cred["hash"], "se": 7,
            }]},
        )
        assert r.status_code == 200 and r.json()["imported"] == [name]
        got = client.get(f"/internal/accounts/{name}", headers=auth).json()["account"]
        assert got["role"] == "admin" and got["se"] == 7
        assert client.post(f"/internal/accounts/{name}/verify", headers=auth, json={"password": PW}).status_code == 200
        assert client.post(
            f"/internal/accounts/{name}/verify", headers=auth, json={"password": "别的口令"}
        ).status_code == 401
    finally:
        _cleanup(client, name)
