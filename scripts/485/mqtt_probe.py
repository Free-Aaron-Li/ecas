#!/usr/bin/env python3
"""MQTT broker 探测器 —— 判断某个 host:port 到底是不是 MQTT broker。

用法:
    python mqtt_probe.py                          # 默认探测 47.99.152.116:10067
    python mqtt_probe.py 124.222.122.146 1883     # 探测自己的服务器

原理:
    发一个最精简的 MQTT 3.1.1 CONNECT 包（空 ClientId + 清除会话），看对面回什么：

      20 02 00 00            -> 是 MQTT broker，且接受匿名连接        ✅
      20 02 00 01/02/04/05   -> 是 MQTT broker，但被拒（需凭据）      ⚠️
      回显原包 / 空 / 超时    -> 不是 MQTT broker（echo 服务器等）     ❌
      15 03 03 ...           -> 该端口要求 TLS

背景（ECAS 现场 09-25 实测）:
    DTU 里配的 47.99.152.116:10067 实测「原样回显」，判定为 TCP 回声服务器，
    不是 broker —— 这正是 DTU 日志里 +STATUS: 1, MQTT CONNECTED / MQTT CLOSED
    反复抖动的原因（DTU 收到的是自己发出去的包）。
"""
import socket
import sys

HOST = sys.argv[1] if len(sys.argv) > 1 else '47.99.152.116'
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 10067

# MQTT 3.1.1 CONNECT 包（14 字节）
#   10         CONNECT
#   0c         remaining length = 12
#   00 04 MQTT protocol name
#   04         protocol level 4 (MQTT 3.1.1)
#   02         connect flags = clean session
#   00 3c      keepalive = 60s
#   00 00      client id = 空
CONNECT = bytes.fromhex('100c00044d5154540402003c0000')

REASONS = {
    '00': '连接已接受 ✅ 这个 broker 允许匿名连接',
    '01': '拒绝：不支持的协议版本',
    '02': '拒绝：ClientId 不被接受',
    '03': '拒绝：服务不可用',
    '04': '拒绝：用户名或密码格式错误',
    '05': '拒绝：未授权（需要正确的账号 / 密码）',
}


def main():
    print(f'→ 探测 {HOST}:{PORT}')

    try:
        s = socket.create_connection((HOST, PORT), timeout=5)
    except Exception as e:
        print(f'❌ TCP 连不上: {e}')
        print('   判断：端口没开 / 防火墙挡了 / 地址写错了')
        return
    print('✅ TCP 已连接')

    s.sendall(CONNECT)
    s.settimeout(5)
    try:
        data = s.recv(64)
    except Exception:
        data = b''
    s.close()

    if not data:
        print('❌ 5 秒内没有回复')
        print('   判断：不是 MQTT broker（或它等的是别的协议 / 需要先发别的握手）')
        return

    hexs = data.hex()
    print(f'← 收到 {len(data)} 字节: {hexs}')

    if hexs == CONNECT.hex():
        print('❌ 判定：不是 MQTT broker —— 这是【TCP 回声服务器】（原样回显）')
        print('   典型来源：`nc -lk <port>` 之类的监听，或厂商的 TCP 回环测试服务器')
        return

    if hexs.startswith('2002'):
        rc = hexs[6:8]
        print(f'✅ 判定：是 MQTT broker（CONNACK return code = 0x{rc}）')
        print(f'   {REASONS.get(rc, "未知返回码")}')
        return

    if hexs.startswith('150303'):
        print('⚠️ 判定：这个端口要求 TLS（收到 TLS 告警）')
        print('   试试 8883 端口，或在 DTU 侧打开 MQTTSSL')
        return

    print('❓ 判定：收到非 MQTT 回复 —— 大概率不是 MQTT broker（可能是私有协议）')


if __name__ == '__main__':
    main()
