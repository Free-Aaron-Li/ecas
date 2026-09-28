# ============================================================================
# ECAS 采集器镜像 —— 只做两件事：
#   1) 用 paho 从云端 Mosquitto(8884/TLS) 取数（只读账号 ecas-monitor，口令走文件）
#   2) 管道喂给现成的 dpro66_monitor.sh --pipe（解析/去重/换算/入库一字不改）
#
# 构建上下文里必须有 dpro66_monitor.sh：它**不复制**在仓库里，由 `deploy.sh`
# 在推送时从唯一来源 `scripts/485/dpro66_monitor.sh` 拷进来，避免两份脚本漂移。
# ============================================================================
FROM python:3.13-slim

# 目标机（腾讯云 CVM）连不上 Docker Hub 与默认 PyPI/apt，构建时走国内镜像
ARG PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
ARG APT_MIRROR=mirrors.tuna.tsinghua.edu.cn

RUN set -eux; \
    if [ -n "$APT_MIRROR" ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g; s|security.debian.org|${APT_MIRROR}|g" \
            /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        postgresql-client ca-certificates tzdata; \
    rm -rf /var/lib/apt/lists/*; \
    pip install --no-cache-dir -i "$PIP_INDEX" paho-mqtt

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1

WORKDIR /opt/ecas
COPY mqtt_tls_feeder.py entrypoint.sh dpro66_monitor.sh ./
RUN chmod +x entrypoint.sh dpro66_monitor.sh

ENTRYPOINT ["./entrypoint.sh"]
