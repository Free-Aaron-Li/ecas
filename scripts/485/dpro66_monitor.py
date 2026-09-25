#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DPRO-66 浓度计 —— MQTT 实时监视 + 浓度换算 + 记录
ECAS / 2026-09-25

链路：
  浓度计 ──4~20mA──▶ 中盛AI模块 ──485 Modbus RTU──▶ 塔石DTU ──MQTT──▶ Mosquitto ──▶ 本脚本

数据帧格式（DTU 在「数据转换 → Modbus TCP」下把 RTU 转成了 TCP：去掉 CRC、加 6 字节 MBAP 头）：
  00 00 | 00 00 | 00 05 | 01 | 04 | 02 | <hi> <lo>
  TID     PID=0   LEN=5   从站  功能码  字节数   寄存器值（大端）
  ⇒ raw = payload[9] * 256 + payload[10]

换算：
  mA     = raw / 1000
  未补偿 = (mA - I_lo) / (I_hi - I_lo) * (C_hi - C_lo) + C_lo    # 默认 4~20mA ↔ 0~100
         = (raw - 4000) / 160
  浓度   = 未补偿 * gain + offset                                # ⭐ 默认 gain=1.0, offset=+0.3（09-26 定案）
         （09-25 用户定案：在【换算出的浓度】上【多加 0.5 个浓度点】——
           例：52.345 → 52.845。⚠️ 不是 ×1.005，那是「加自身的 0.5%」，只有 +0.26）
  活零 live zero：raw < 3600（<3.6mA）= 断线/仪表故障；raw > 20500 = 超量程
                —— 这两种情况都不能当浓度看

用法（三种，任选）：

  1) ⭐ 直接跑 —— 脚本自己拉起 mosquitto_sub（推荐，最省事）：
       python3 dpro66_monitor.py

  2) 手工管道 —— 自己接 mosquitto_sub 的输出：
       mosquitto_sub -h 127.0.0.1 -p 1883 -t 'dpro66' -F '%I|%x' \
           | python3 dpro66_monitor.py

  3) 回放已抓到的日志：
       cat /root/mqtt-capture.log | python3 dpro66_monitor.py

  （另有 --broker 模式可直接连 broker，需要 pip3 install paho-mqtt，一般用不到）

按 Ctrl-C 结束并打印统计。
"""

import argparse
import csv
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime

# ----------------------------------------------------------------- 常量
BREAK_LOW = 3600       # < 3.6 mA  → 断线 / 仪表故障
OVER_HIGH = 20500      # > 20.5 mA → 超量程

HEARTBEATS = (b'Q', b'A', b'ping', b'PING')

ROW_FMT = '{t:<28} {raw:<8} {ma:<9} {c:<9} {cu:<9} {st}'

BANNER = """\
================================================================================
  DPRO-66 浓度计实时监视                        ECAS / 2026-09-25
--------------------------------------------------------------------------------
  换算     未补偿 = (raw - {rlo}) / {div}          <- {i_lo}~{i_hi} mA  <->  {c_lo}~{c_hi}
  修正     ⭐ 屏幕「浓度」列 = 最终值：{rule}
           （09-25 定案：在换算出的浓度上【多加 {offset} 个浓度点】，不是乘 1.005；
             例 52.345 → 52.845。电流读数与 (raw-4000)/160 换算均不变）
  ⚠️ 断线判据 raw < {bl}（<3.6mA） ｜ 超量程 raw > {oh}（>20.5mA）
  数据来源 {source}
  记录文件 {csvfile}
--------------------------------------------------------------------------------
  👉 请在浓度计本体上读一下当前显示值，与下面的「浓度」列直接对照
================================================================================"""


# ----------------------------------------------------------------- 换算
def raw_to_ma(raw):
    return raw / 1000.0


def ma_to_conc(ma, i_lo, i_hi, c_lo, c_hi):
    if i_hi == i_lo:
        return float('nan')
    return (ma - i_lo) / (i_hi - i_lo) * (c_hi - c_lo) + c_lo


def status_of(raw):
    if raw < BREAK_LOW:
        return '⚠️ 断线/仪表故障'
    if raw > OVER_HIGH:
        return '⚠️ 超量程·不可信'
    return '正常'


# ----------------------------------------------------------------- 解析
def decode_frame(payload):
    """只认 DTU 转出来的 Modbus TCP 数据帧；不是就返回 None

       00 00 | 00 00 | 00 05 | 01 | 04 | 02 | hi lo
    """
    if len(payload) != 11:
        return None
    length = (payload[4] << 8) | payload[5]
    unit, fc, nbytes = payload[6], payload[7], payload[8]
    if length != 5 or fc != 0x04 or nbytes != 2:
        return None
    return {
        'tid': (payload[0] << 8) | payload[1],
        'unit': unit,
        'fc': fc,
        'raw': (payload[9] << 8) | payload[10],
    }


def describe_other(payload):
    """非数据帧（注册包 / 心跳 / 其它）的说明文字"""
    if payload in HEARTBEATS:
        return '心跳 %s' % payload.decode('ascii', 'replace')
    if len(payload) == 4 and payload.isalpha():
        return '心跳 %s' % payload.decode('ascii', 'replace')
    try:
        txt = payload.decode('ascii')
        if txt.isprintable() and len(txt) <= 40:
            return '文本 %s' % txt
    except UnicodeDecodeError:
        pass
    return '其它 %d 字节' % len(payload)


def extract(line):
    """从 mosquitto_sub 的一行里抠出 (时间戳, payload bytes)；抠不出返回 None"""
    line = line.rstrip('\r\n')
    if not line.strip():
        return None

    ts, tok = '', None
    m = re.search(r'hex=([0-9a-fA-F]+)', line)
    if m:
        tok = m.group(1)
        ts = line.split('|')[0].strip()
        if 'hex=' in ts or ts == line.strip():
            ts = ''
    else:
        parts = [p.strip() for p in line.split('|')]
        if len(parts) >= 2:
            ts = parts[0]
        for p in reversed(parts):
            if re.fullmatch(r'[0-9a-fA-F]+', p) and len(p) % 2 == 0:
                tok = p
                break
        if tok is None:
            tok = line.strip()
        if not re.fullmatch(r'[0-9a-fA-F]+', tok):
            return None

    if not tok or len(tok) % 2:
        return None
    try:
        return ts, bytes.fromhex(tok)
    except ValueError:
        return None


# ----------------------------------------------------------------- 主程序
class Monitor(object):
    def __init__(self, args):
        self.a = args
        span = args.c_hi - args.c_lo
        # 浓度 = (raw - i_lo*1000) / div   其中 div = 1000 * (i_hi-i_lo) / (c_hi-c_lo)
        # 4~20mA ↔ 0~100 时 div = 160
        self.div = (1000.0 * (args.i_hi - args.i_lo) / span) if span else 160.0
        self.raw_at_lo = args.i_lo * 1000.0
        self.n_data = 0
        self.n_ok = 0
        self.n_other = 0
        self.n_zero = 0          # 收到帧、但值 = 0（4~20mA 断线 / 仪表失电）
        self.n_bad = 0           # 收到帧、但越限
        self.t_start = time.time()
        self.t_last_note = self.t_start
        self.cmin = None
        self.cmax = None
        self.csum = 0.0
        self.t_first = None
        self.t_last = None
        self.csv_f = None
        self.writer = None
        self._open_csv()

    # -------------------------------------------------- csv
    def _open_csv(self):
        if self.a.no_csv:
            return
        new = (not os.path.exists(self.a.csv)) or os.path.getsize(self.a.csv) == 0
        self.csv_f = open(self.a.csv, 'a', newline='', encoding='utf-8')
        self.writer = csv.writer(self.csv_f)
        if new:
            self.writer.writerow(['时间', 'raw', 'mA', '浓度', '未补偿',
                                  '状态', '原始payload'])
            self.csv_f.flush()

    # -------------------------------------------------- 一行
    def feed(self, line):
        got = extract(line)
        if got is None:
            return
        ts, payload = got
        if ts:
            self.t_last = ts
            if self.t_first is None:
                self.t_first = ts

        frame = decode_frame(payload)
        if frame is None:
            self.n_other += 1
            print('{t:<28} · {d}  ({n} 字节, {hexs})'.format(
                t=ts or datetime.now().strftime('%H:%M:%S'),
                d=describe_other(payload),
                n=len(payload),
                hexs=payload.hex(),
            ))
            sys.stdout.flush()
            # ⏳ 还没见到任何数据帧时，每分钟给一句进度提示（免得以为脚本卡死了）
            now = time.time()
            if self.n_data == 0 and now - self.t_last_note >= 60:
                self.t_last_note = now
                print('   ⏳ 已等待 %d 秒，收到 %d 条消息（心跳/注册包），但还没有数据帧'
                      % (int(now - self.t_start), self.n_other))
                print('      心跳每 120 秒一条 ⇒ 链路是活的；数据上传周期约 100 秒，'
                      '请再耐心等几分钟（Ctrl-C 可随时收尾）')
                sys.stdout.flush()
            return

        raw = frame['raw']
        ma = raw_to_ma(raw)
        c = ma_to_conc(ma, self.a.i_lo, self.a.i_hi, self.a.c_lo, self.a.c_hi)
        # ⭐ 浓度修正（09-25 用户定案）：在换算出的浓度上【多加 0.5 个浓度点】——
        #    加性偏移，不是乘性百分比（×1.005 只有 +0.26，先生要的是 +0.5）
        cg = c * self.a.gain + self.a.offset
        st = status_of(raw)
        ok = (st == '正常')

        self.n_data += 1
        if ok:
            self.n_ok += 1
            # ⭐ 统计一律基于【补偿后】的浓度（先生拿它对浓度计本体）
            self.csum += cg
            self.cmin = cg if self.cmin is None else min(self.cmin, cg)
            self.cmax = cg if self.cmax is None else max(self.cmax, cg)

        print(ROW_FMT.format(
            t=ts or datetime.now().strftime('%H:%M:%S'),
            raw=raw, ma='%.3f' % ma,
            c=('%.3f' % cg) if ok else '—',      # ⭐ 浓度列 = 已补偿的最终值
            cu=('%.3f' % c) if ok else '—',      # 未补偿（仅供溯源）
            st=st,
        ))
        sys.stdout.flush()

        # ⭐ 收到帧但值不正常时，大声喊出来（只喊前几次，避免刷屏）
        if not ok:
            if raw == 0:
                self.n_zero += 1
                if self.n_zero <= 3:
                    print('   🔴 模块报 0.000 mA —— 这不是「没数据」，是 4~20mA 回路断了'
                          '或浓度计失电/没输出！')
                    print('      （4mA 才代表浓度 0；0mA 说明信号根本没上来 —— 活零的意义就在这）')
                    print('      👉 万用表 mA 档【串联】量回路电流 ／ 拔掉 DTU 的 A/B 后用'
                          '中盛软件读全部 18 路，看是不是只有 CH1 为 0')
            else:
                self.n_bad += 1
                if self.n_bad <= 3:
                    print('   ⚠️ 越限读数 raw=%d → %.3f mA（%s）—— 检查量程制式与接线'
                          % (raw, ma, st))
            sys.stdout.flush()

        if self.writer:
            self.writer.writerow([ts, raw, '%.3f' % ma,
                                  ('%.3f' % cg) if ok else '',
                                  ('%.3f' % c) if ok else '',
                                  st, payload.hex()])
            self.csv_f.flush()

    # -------------------------------------------------- 收尾
    def summary(self):
        print('\n' + '-' * 80)
        print('  数据帧 %d 个（正常 %d ｜ 异常 %d） ｜ 其它(注册包/心跳等) %d 条'
              % (self.n_data, self.n_ok, self.n_data - self.n_ok, self.n_other))
        if self.n_ok:
            print('  浓度 最低 %.3f ｜ 最高 %.3f ｜ 平均 %.3f   （✅ 已含修正：×%.4g %+.3f）'
                  % (self.cmin, self.cmax, self.csum / self.n_ok, self.a.gain, self.a.offset))
            print('  区间  %s  →  %s' % (self.t_first, self.t_last))
            print('  ⭐ 请把这段「浓度」与浓度计本体显示值对照 —— 它已经是补偿后的最终值')
        elif self.n_data:
            # ⭐ 收到帧了，但一个正常的都没有 —— 链路没问题，问题在模拟量侧
            if self.n_zero:
                print('  🔴 收到 %d 个数据帧，但【全部是 0.000 mA】—— 通信链路完全正常，'
                      '问题在模拟量侧：' % self.n_zero)
                print('     · 4~20mA 回路断线 / 接线松脱（模块端或浓度计端）')
                print('     · 浓度计（变送器 TRA-9861）失电、关机、或没有输出')
                print('     · 接错通道（CH1=0000H … CH18=0011H，读全部 18 路看哪一路有值）')
                print('     👉 ① 万用表 mA 档【串联】进回路量电流（⚠️ 不可并联）')
                print('        ② 拔掉 DTU 的 A/B（RS485 单主站），用中盛软件整块读 18 路')
            else:
                print('  ⚠️ 收到 %d 个数据帧，但没有一个落在正常区间（%d 个越限）—— '
                      '检查量程制式（0~20mA 还是 4~20mA）与接线' % (self.n_data, self.n_bad))
        else:
            print('  ⚠️ 一个数据帧都没收到 —— 检查：')
            print('     0) 是不是把脚本直接跑了、没接数据源？（直接跑会自动拉起 mosquitto_sub，'
                  '若报「找不到 mosquitto_sub」就用管道方式）')
            print('     1) mosquitto_sub 是否挂在正确的主题上')
            print('     2) DTU 的「推送主题1」是否填了 dpro66')
            print('     3) DTU 的「主题1开关」是否选的是 qos=0/1/2（选「关闭推送」就是关）')
            print('     4) DTU 南向轮询是否开着（采集开关 = 自定义轮询采集），'
                  '指令 01040000000131CA')
            print('     5) ⚠️ 上传周期约 100 秒（界面里没有这个参数，第一嫌疑是 '
                  '+DTUPACKET: 50,2500）—— 至少让脚本跑 3~5 分钟再看')
            print('     6) ⭐ 心跳在但不来数据帧 ⇒ DTU 没拿到 485 数据：'
                  '看 DTU 上 485 的 TX/RX 灯闪不闪')
        if self.csv_f:
            print('  记录已写入 %s' % os.path.abspath(self.a.csv))
        print('-' * 80)

    def close(self):
        if self.csv_f:
            try:
                self.csv_f.close()
            except Exception:
                pass
            self.csv_f = None


def spawn_mosquitto(args):
    """自己拉起 mosquitto_sub，把它的 stdout 当输入流。

    这样先生直接 `python3 dpro66_monitor.py` 就能用，不用手写管道。
    返回 Popen 对象；mosquitto_sub 不存在时返回 None。
    """
    exe = shutil.which('mosquitto_sub')
    if exe is None:
        return None
    cmd = [exe, '-h', args.host, '-p', str(args.port), '-t', args.topic, '-F', '%I|%x']
    if args.username:
        cmd += ['-u', args.username]
    if args.password:
        cmd += ['-P', args.password]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, bufsize=1)


def main():
    ap = argparse.ArgumentParser(
        description='DPRO-66 浓度计 MQTT 监视 + 浓度换算'
                    '（默认自己拉起 mosquitto_sub；也可用管道喂它）')
    ap.add_argument('-t', '--topic', default='dpro66', help='MQTT 主题（09-25 起改为短名 dpro66）')
    ap.add_argument('--host', default='127.0.0.1', help='broker 地址（默认 127.0.0.1）')
    ap.add_argument('--port', type=int, default=1883, help='broker 端口（默认 1883）')
    ap.add_argument('--username', default=os.environ.get('MQTT_USER') or None,
                    help='MQTT 用户名（也可用环境变量 MQTT_USER）')
    ap.add_argument('--password', default=os.environ.get('MQTT_PASS') or None,
                    help='MQTT 密码（也可用环境变量 MQTT_PASS）')
    ap.add_argument('--pipe', action='store_true',
                    help='强制从 stdin 读（默认：stdin 是管道就自动用管道，是终端就自己起 mosquitto_sub）')
    ap.add_argument('--broker', default=None, help='用 paho-mqtt 直连 broker（需 pip3 install paho-mqtt）')
    ap.add_argument('--replay', action='store_true', help='回放 stdin')
    ap.add_argument('--i-lo', type=float, default=4.0, help='电流下限 mA（默认 4）')
    ap.add_argument('--i-hi', type=float, default=20.0, help='电流上限 mA（默认 20）')
    ap.add_argument('--c-lo', type=float, default=0.0, help='浓度下限（默认 0）')
    ap.add_argument('--c-hi', type=float, default=100.0, help='浓度上限（默认 100）')
    ap.add_argument('-o', '--offset', type=float, default=0.3,
                    help='⭐ 浓度加性偏移（09-25 定案：在换算出的浓度上多加 0.5 个浓度点，'
                         '例 52.345 → 52.845；默认 +0.5，设 0 关闭）')
    ap.add_argument('-g', '--gain', type=float, default=1.0,
                    help='浓度乘性系数（默认 1，一般不动；⚠️ ×1.005 只等于加自身的 0.5%%，不是 +0.5）')
    ap.add_argument('--csv', default='dpro66_readings.csv', help='记录文件（默认 dpro66_readings.csv）')
    ap.add_argument('--no-csv', action='store_true', help='不写记录文件')
    args = ap.parse_args()

    # ---- 决定数据来源 ----
    use_pipe = (args.replay or args.pipe or args.broker is None) and not sys.stdin.isatty()
    if args.pipe or args.replay:
        use_pipe = True
    if args.broker:
        source = 'paho-mqtt → %s:%d 「%s」' % (args.broker, args.port, args.topic)
    elif use_pipe:
        source = 'stdin（管道 / 回放）'
    else:
        source = 'mosquitto_sub 子进程 → %s:%d 「%s」' % (args.host, args.port, args.topic)

    mon = Monitor(args)
    print(BANNER.format(
        rlo=int(args.i_lo * 1000), div=('%.6g' % mon.div),
        i_lo=args.i_lo, i_hi=args.i_hi, c_lo=args.c_lo, c_hi=args.c_hi,
        gain=('%.4g' % args.gain), offset=('%g' % args.offset),
        rule=('浓度 = 未补偿 %+g' % args.offset) if args.gain == 1.0
             else ('浓度 = 未补偿 × %.4g %+g' % (args.gain, args.offset)),
        bl=BREAK_LOW, oh=OVER_HIGH, source=source,
        csvfile=('(不写)' if args.no_csv else os.path.abspath(args.csv)),
    ))
    print(ROW_FMT.format(t='时间', raw='raw', ma='mA', c='浓度', cu='未补偿', st='状态'))
    print('-' * 80)
    sys.stdout.flush()

    proc = None
    try:
        if args.broker:
            try:
                import paho.mqtt.client as mqtt
            except ImportError:
                print('需要 paho-mqtt：pip3 install paho-mqtt\n'
                      '或者改用零依赖方式（直接跑）：python3 %s'
                      % os.path.basename(__file__),
                      file=sys.stderr)
                return 2

            def on_message(client, userdata, msg):
                mon.feed('%s|%s' % (datetime.now().strftime('%Y-%m-%dT%H:%M:%S'), msg.payload.hex()))

            try:
                cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                                  client_id='ecas-monitor-%d' % os.getpid())
            except (AttributeError, TypeError):
                cli = mqtt.Client(client_id='ecas-monitor-%d' % os.getpid())
            cli.on_message = on_message
            cli.connect(args.broker, args.port, 60)
            cli.subscribe(args.topic, qos=0)
            cli.loop_forever()

        elif use_pipe:
            for line in sys.stdin:
                mon.feed(line)

        else:
            # ⭐ 自己拉起 mosquitto_sub
            proc = spawn_mosquitto(args)
            if proc is None:
                print('❌ 找不到 mosquitto_sub —— 请先安装，或改用管道方式：\n'
                      "   mosquitto_sub -h %s -p %d -t '%s' -F '%%I|%%x' | python3 %s"
                      % (args.host, args.port, args.topic, os.path.basename(__file__)),
                      file=sys.stderr)
                return 2
            for line in proc.stdout:
                mon.feed(line)
            proc.wait()

    except KeyboardInterrupt:
        pass
    finally:
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
                err = proc.stderr.read()
                if err and err.strip():
                    print('\n⚠️ mosquitto_sub 的输出：\n%s' % err.strip(), file=sys.stderr)
            except Exception:
                pass
        mon.summary()
        mon.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
