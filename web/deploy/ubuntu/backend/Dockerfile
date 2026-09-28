# ============================================================================
# ECAS 后端镜像（FastAPI + psycopg）—— 依赖按 `uv.lock` 冻结安装
#
# ⚠️ 目录层级必须与仓库一致：`app/main.py:90` 用
#      DIST_DIR = Path(__file__).resolve().parents[2] / "frontend" / "dist"
#    算前端产物位置 ⇒ 容器里后端放 `/srv/web/backend`，前端产物挂到
#    `/srv/web/frontend/dist`（见 docker-compose.yml 的 api.volumes）。
#    若改成 /app 之类的路径，parents[2] 会变成 `/`，前端就 404 了。
# ============================================================================
FROM python:3.13-slim

ARG PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_DEFAULT_INDEX=${PIP_INDEX}

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates tzdata; \
    rm -rf /var/lib/apt/lists/*; \
    pip install --no-cache-dir -i "$PIP_INDEX" uv

WORKDIR /srv/web/backend
# 先只拷依赖清单，改动代码不会让依赖层失效
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev

COPY app ./app
COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh

ENV PATH="/srv/web/backend/.venv/bin:$PATH"
EXPOSE 8000

# entrypoint 负责把 container secret 变成 PGPASSFILE + ECAS_AUTH_TOKEN，再 exec 这个 CMD
ENTRYPOINT ["./entrypoint.sh"]
# ECAS_HOST/ECAS_PORT 由 compose 的 environment 注入（实际监听 0.0.0.0:8000）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
