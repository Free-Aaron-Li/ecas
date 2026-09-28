#!/usr/bin/env python3
"""ECAS 采集端 · 从云端 Mosquitto(8884/TLS) 取数，按 `mosquitto_sub -F '%I|%x'` 的格式打到 stdout。

为什么不用 `mosquitto_sub`：
  1. 容器里不必再装 mosquitto-clients；
  2. **口令不进 argv**（mosquitto_sub 只能 `-P <明文>`，`ps`/`/proc/*/cmdline` 可见），
     这里从文件读（默认 `/run/secrets/mqtt-pass`，见 compose 的 secrets 挂载）；
  3. 断线自动重连、MQTT 保活与会话恢复交给 paho，比 shell 里 `while true` 重启可靠。

输出格式与 `%I|%x` 逐字一致 —— `2026-09-26T19:30:00+0800|<payload 的小写十六进制>`，
因此可以原样管道给现成的 `dpro66_monitor.sh --pipe`（解析、换算、去重、入库一概不改）。

用法：
  ./mqtt_tls_feeder.py --host 10.10.10.123 --port 8884 --topic dpro66 \
      --username ecas-monitor --password-file /run/secrets/mqtt-pass --cafile /etc/ecas/ca.crt \
      --cert-sha256 B6:4B:8D:... \
    | ./dpro66_monitor.sh --pipe --model DPRO-66 --no-csv --db-dsn "-h db -U ecas -d ecas"

TLS 校验说明：云端 `server.crt` 的 SAN 只有 `IP:124.222.122.146`、`IP:127.0.0.1`、
`DNS:VM-0-13-opencloudos`，**没有 mesh 地址 10.10.10.123**；而腾讯云安全组没放行 8884
（云端配置注释自己写着「未放行安全组 ⇒ 公网不可达，仅作就绪备用」），测试机走公网连不上。
⇒ 改走 **mesh 连 `10.10.10.123:8884`**（目标机实测可达）。此时主机名校验必然失败，
所以用「**CA 链校验 + 证书指纹钉扎**」这一等价替代：

  · `ctx.verify_mode = CERT_REQUIRED` —— 仍要求证书由 ECAS CA 签发（链校验照旧）；
  · 只关掉 hostname 检查，改用 `--cert-sha256` 比对对端 leaf 证书的 SHA-256 指纹；
  · 指纹取不到、或不匹配 ⇒ 抛 CertPinError，**拒绝订阅**（fail closed，宁可不取数）。

云端若重签 `server.crt`（如给 SAN 加 mesh 地址），指纹会变 ⇒ 同步更新 `MQTT_CERT_SHA256`。
取当前指纹：`ssh ecas openssl x509 -in /etc/mosquitto/certs/server.crt -noout -fingerprint -sha256`
"""

from __future__ import annotations

import argparse
import hashlib
import os
import pathlib
import ssl
import sys
import time
from datetime import datetime, timedelta, timezone

import paho.mqtt.client as mqtt

# 现场时间一律东八区（与 mosquitto_sub 所在机器的本地时区一致）
CST = timezone(timedelta(hours=8))


def read_password(path_arg: str | None) -> str:
    """口令只从文件或环境取，绝不从命令行参数取（那样会出现在 ps 里）。"""
    path = path_arg or os.environ.get("MQTT_PASS_FILE", "")
    if path:
        try:
            return pathlib.Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SystemExit(f"✋ 读不到口令文件 {path}：{exc}") from exc
    env = os.environ.get("MQTT_PASS", "").strip()
    if env:
        return env
    raise SystemExit("✋ 没有口令：请给 --password-file 或设 MQTT_PASS_FILE / MQTT_PASS")


def make_client(client_id: str):
    """兼容 paho-mqtt 1.x / 2.x 的构造方式（2.x 不带回调版本会告警）。"""
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=client_id, clean_session=False)
    except AttributeError:  # paho 1.x
        return mqtt.Client(client_id=client_id, clean_session=False)


class CertPinError(RuntimeError):
    """证书指纹校验失败 —— 拒绝订阅：宁可不取数，也不接收不可信服务端的数据。"""


def normalize_fp(value: str) -> str:
    """把 `B6:4B:...` / `b64b...` / 带空格 的指纹统一成 64 位小写十六进制。"""
    return value.replace(":", "").replace(" ", "").strip().lower()


def peer_cert_fingerprint(client) -> str | None:
    """取本次 TLS 连接对端证书的 SHA-256 十六进制；取不到返回 None。"""
    try:
        der = client._sock.getpeercert(binary_form=True)  # paho 内部持有已包装的 SSLSocket
    except Exception:  # noqa: BLE001 —— 私有属性/套接字状态都可能出问题，一律视为「取不到」
        return None
    return hashlib.sha256(der).hexdigest() if der else None


def main() -> int:
    ap = argparse.ArgumentParser(description="云端 Mosquitto(TLS) → stdout(`ts|hex` 行)")
    ap.add_argument("--host", default=os.environ.get("MQTT_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("MQTT_PORT", "8884")))
    ap.add_argument("--topic", default=os.environ.get("MQTT_TOPIC", "dpro66"))
    ap.add_argument("--username", default=os.environ.get("MQTT_USER", "ecas-monitor"))
    ap.add_argument("--password-file", default=os.environ.get("MQTT_PASS_FILE"))
    ap.add_argument("--cafile", default=os.environ.get("MQTT_CAFILE", ""), help="CA 证书；留空=明文 MQTT（仅本机调试）")
    ap.add_argument("--cert-sha256", default=os.environ.get("MQTT_CERT_SHA256", ""),
                    help="钉扎服务端证书的 SHA-256 指纹（给了就关主机名校验、改用指纹校验；必须同时给 --cafile）")
    ap.add_argument("--client-id", default=os.environ.get("MQTT_CLIENT_ID", "dpro66-collector-ubuntu"))
    ap.add_argument("--qos", type=int, choices=(0, 1, 2), default=1)
    ap.add_argument("--keepalive", type=int, default=60)
    ap.add_argument("--verbose", action="store_true", help="把连接/订阅事件打到 stderr（docker logs 里看得到）")
    args = ap.parse_args()

    def log(msg: str) -> None:
        if args.verbose:
            print(f"[feeder] {msg}", file=sys.stderr, flush=True)

    pin = normalize_fp(args.cert_sha256)
    client = make_client(args.client_id)
    client.username_pw_set(args.username, read_password(args.password_file))
    if pin and not args.cafile:
        raise SystemExit("✋ 给了 --cert-sha256 就必须同时给 --cafile：没有 CA 就谈不上校验，钉扎不是替代品")
    if args.cafile:
        if pin:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.verify_mode = ssl.CERT_REQUIRED   # 链校验照旧：必须是 ECAS CA 签发的
            ctx.check_hostname = False            # SAN 里没有 mesh 地址 ⇒ 用指纹钉扎替代
            ctx.load_verify_locations(args.cafile)
            client.tls_set_context(ctx)
            log(f"TLS 证书：{args.cafile}（CA 链校验 + 指纹钉扎 {pin[:16]}…，不校验主机名）")
        else:
            client.tls_set(ca_certs=args.cafile)
            log(f"TLS 证书：{args.cafile}（标准校验：链 + 主机名）")
    else:
        log("⚠️ 未指定 CA，明文连接")
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    def on_connect(_c, _u, _f, rc):
        if rc != 0:
            print(f"[feeder] 连接被拒 rc={rc}（4=用户名或口令错，5=无权限，见 ACL）", file=sys.stderr, flush=True)
            return
        if pin:
            got = peer_cert_fingerprint(_c)
            if got is None:
                raise CertPinError("拿不到对端证书指纹 ⇒ 拒绝订阅（钉扎校验失败）")
            if got != pin:
                raise CertPinError(
                    f"证书指纹不匹配：期望 {pin[:16]}… 实得 {got[:16]}…；"
                    "云端若重签过 server.crt，请按 .env.example 的方法更新 MQTT_CERT_SHA256"
                )
            log(f"证书指纹 ✓ {got[:16]}…")
        _c.subscribe(args.topic, qos=args.qos)
        log(f"已连上 {args.host}:{args.port}，订阅 {args.topic} (QoS{args.qos})")

    def on_disconnect(_c, _u, rc):
        log(f"断开（rc={rc}），paho 会自动重连")

    def on_message(_c, _u, msg):
        ts = datetime.now(CST).strftime("%Y-%m-%dT%H:%M:%S%z")
        sys.stdout.write(f"{ts}|{msg.payload.hex()}\n")
        sys.stdout.flush()

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    while True:  # 连不上（网络抖动/DNS/服务端重启）就退避重试，别让容器直接退出
        try:
            client.connect(args.host, args.port, keepalive=args.keepalive)
            client.loop_forever(retry_first_connection=False)
        except KeyboardInterrupt:
            return 0
        except Exception as exc:  # noqa: BLE001 —— 任何异常都只记日志、继续重试
            print(f"[feeder] 异常：{exc.__class__.__name__}: {exc}；5 秒后重试", file=sys.stderr, flush=True)
            time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
