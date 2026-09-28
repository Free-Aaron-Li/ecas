"""测试会话级前置。

生产是 fail-closed（`ECAS_AUTH_TOKEN` 缺失 ⇒ `/api/*` 一律 503、容器入口直接拒绝启动，
见 2026-09-28 安全审查 M-03）；测试进程本来就没有令牌，所以在 `app.main` 被导入
（导入时就会构造 FastAPI 并读一次 settings）之前，显式声明「本机开发模式」。

⚠️ 必须在 conftest 里设：pytest 会先导入 conftest，再收集测试模块，
   而各测试模块是**在 fixture 里**才 `from app.main import app` 的。
"""

from __future__ import annotations

import os

os.environ.setdefault("ECAS_ALLOW_NO_AUTH", "1")
