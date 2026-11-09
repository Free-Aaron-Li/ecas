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

# 中文字体（⚠️ 不是可选项，是班次报表 Excel 的正确性依赖）
#   app/report.py 用 matplotlib 把曲线图渲成 PNG 嵌进 .xlsx。matplotlib 找不到含中文字形的
#   字体时会**静默退化**成英文标签（宁可英文也不要一片方块）——功能不报错、图也出得来，
#   但先生要的是「网页上展示的全部信息」，中文标签是其中一部分，退化了不易察觉。
#   python:3.13-slim 里连 /usr/share/fonts 目录都不存在，所以必须在这里装。
#   fonts-noto-cjk 落到 /usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc，
#   正是 report.py `_CJK_FONT_PATHS` / `_CJK_FONT_GLOBS` 覆盖的路径。
#   fontconfig 不是 matplotlib 的依赖（report.py 直接按文件路径 addfont），
#   装它是为了容器里能跑 `fc-list :lang=zh` 做人工核对 —— 排查成本远低于多出来的几 MB。
#   注意：不要往里塞 `fonts-noto-cjk-extra` / `fonts-noto-color-emoji`，本报表用不到。
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates tzdata fontconfig fonts-noto-cjk; \
    fc-cache -f >/dev/null; \
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
