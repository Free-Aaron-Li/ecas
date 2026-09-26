#!/usr/bin/env bash
# -*- coding: utf-8 -*-
#
# DPRO-66 浓度计 —— MQTT 实时监视 + 浓度换算 + 记录   （Bash 版 · 零依赖）
# ECAS / 2026-09-25
#
# 与 dpro66_monitor.py 功能等价，但只用 bash + mosquitto_sub + awk，不需要 Python。
#
# 链路：
#   浓度计 ──4~20mA──▶ 中盛AI模块 ──485 Modbus RTU──▶ 塔石DTU ──MQTT──▶ Mosquitto ──▶ 本脚本
#
# 数据帧格式（DTU 在「数据转换 → Modbus TCP」下把 RTU 转成了 TCP：去掉 CRC、加 6 字节 MBAP 头）：
#   00 00 | 00 00 | 00 05 | 01 | 04 | 02 | <hi> <lo>
#   TID     PID=0   LEN=5   从站  功能码  字节数   寄存器值（大端）
#   ⇒ raw = 0x<hi><lo>   ／   mA = raw/1000   ／   浓度 = (raw - 4000) / 160
#
# 活零 live zero：
#   raw < 3600（<3.6mA）  = 断线 / 仪表故障   ← ⚠️ 0mA 不是「浓度 0」，是回路断了
#   raw > 20500（>20.5mA）= 超量程，浓度不可信
#
# 用法：
#   1) ⭐ 直接跑（推荐）—— 脚本自己拉起 mosquitto_sub：
#        ./dpro66_monitor.sh
#   2) 跑 5 分钟就自动收尾打统计（不依赖 timeout，读循环自己计时）：
#        ./dpro66_monitor.sh --duration 300
#   3) 手工管道：
#        mosquitto_sub -h 127.0.0.1 -p 1883 -t 'dpro66' -F '%I|%x' | ./dpro66_monitor.sh --pipe
#   4) 回放已抓到的日志：
#        ./dpro66_monitor.sh --replay /root/mqtt-capture.log
#
# ⭐ 浓度修正（09-26 用户修订：+0.3）—— **加性偏移**，不是乘性百分比！
#   先生原话（09-26）：「实际浓度值 = 换算后的浓度值 + 0.3%」
#   ⇒ 在【换算出的浓度】上直接【加 0.3 个浓度点】：
#        mA        = raw / 1000                     ← 不受修正影响
#        未补偿     = (raw - 4000) / 160             ← 中间量，仅作溯源
#        浓度       = 未补偿 + 0.3                   ← ⭐ 屏幕「浓度」列 = 这个（最终值）
#   实测：raw 12333 → 12.333 mA → 未补偿 52.081 → 浓度 52.381
#         raw 12935 → 12.935 mA → 未补偿 55.844 → 浓度 56.144
#   ⚠️ 曾误做成 浓度 × 1.005（乘性）—— 那是「加自身的 0.5%」，只有 +0.26，不是 +0.5。
#      （09-26 同理：×1.003 只有 +0.15，不是 +0.3）
#      现已改为加性偏移；两个旋钮都在：
#        -o 0.3    偏移（默认 +0.3 个浓度点）      ./dpro66_monitor.sh -o 0    只看未修正
#        -g 1.0    系数（默认 1，一般不动）        ./dpro66_monitor.sh -o 0.7  试别的偏移
#
# 常用参数：-H 主机  -p 端口  -t 主题（默认 dpro66）  -u 用户  -P 密码
#           -c CSV  -o 偏移（默认 +0.3）  -g 系数（默认 1.0）
#           -d 秒数  --no-csv  --i-lo 4 --i-hi 20 --c-lo 0 --c-hi 100
#
# 落库（默认开）：把每条数据帧写进 PostgreSQL 的 raw_reading 表
#           --no-db 关掉  ｜  --db-dsn "-h 127.0.0.1 -U ecas -d ecas" 换连接
#           --dedup 10 同 raw 在 10 秒内重复出现即判为 QoS1 重传、丢弃不入库（--no-dedup 关）
#           --model DPRO-66   设备型号（09-26 定案：库里只认「型号」，不建设备台账）
#           ⭐ 库里**只存三样**：型号 model ｜ 时间 ts ｜ 原始值 raw
#              mA / 浓度 / 补偿 / 断线超量程判定 全部在**后端**做（见 sql/002_queries.sql 的后参数）
#           ⭐ 口令走 /root/.pgpass（600）—— 不进命令行、不进环境变量；psql 用 -w 禁交互，失败即报
#
# 按 Ctrl-C 结束并打印统计。
#
# ⚠️ 跑之前先清掉堆积的订阅进程，否则日志里每条会重复 N 次：
#      pgrep -x mosquitto_sub            # 只看 PID
#      kill -9 <PID> ...                 # ⛔ 别用 ps -ef：它会把 -P 口令原样打进输出
#                                        # ⛔ 别用 pkill -f：经 ssh 时会命中自己的 bash -c
#

set -u

# ----------------------------------------------------------------- 常量
BREAK_LOW=3600          # < 3.6 mA  → 断线 / 仪表故障
OVER_HIGH=20500         # > 20.5 mA → 超量程

# ----------------------------------------------------------------- 默认参数
HOST="127.0.0.1"
PORT="1883"
TOPIC="dpro66"                    # ⭐ 09-25 精简后：dpro66（原 ecas/dpro66/data）
MQTT_USER="${MQTT_USER:-}"        # 环境变量 MQTT_USER / MQTT_PASS 也可
MQTT_PASS="${MQTT_PASS:-}"
CSV="dpro66_readings.csv"
USE_CSV=1
# ---- 落库（PostgreSQL）----
DB_DSN="-h 127.0.0.1 -U ecas -d ecas"   # 口令走 /root/.pgpass
USE_DB=1
DB_MODEL="DPRO-66"      # ⭐ 只记型号；不编号、不分通道（09-26 定案）
DB_WHY=""
GAIN="1.0"              # 乘性系数（一般不动）
OFFSET="0.3"            # ⭐ 09-26 用户定案：在换算浓度上多加 0.3 个浓度点（加性偏移，非 ×1.003）
I_LO="4";  I_HI="20";  C_LO="0";  C_HI="100"
DURATION=""
# ⭐ 09-26：固定 ClientId + 持久会话（-c）⇒ 订阅端短暂离线时 QoS1 帧会被 broker 排队，#    重启/断网不再丢帧。想恢复旧行为用 --no-persist。
CLIENT_ID="dpro66-monitor"
PERSIST=1
QOS="1"
# ⭐ 09-26：QoS1 重传去重窗口。同一 raw 在此秒数内再次出现 ⇒ 判为重传，丢弃不入库
DEDUP_SECONDS="${DEDUP_SECONDS:-10}"
MODE="self"             # self | pipe | replay
REPLAY_FILE=""

# ----------------------------------------------------------------- 统计
n_data=0; n_ok=0; n_other=0; n_zero=0; n_bad=0; n_dup=0
last_raw=""; last_raw_ep=""                 # 去重用的上一条（raw + epoch）
n_db_ok=0; n_db_err=0                      # 落库成功 / 失败计数
cmin=""; cmax=""; csum="0"
t_first=""; t_last=""
t_last_note=$SECONDS
n_gap=0; gap_sum=0; gap_min=""; gap_max=""; t_prev_data=""   # 上报间隔统计
SUMMARY_DONE=0

# ----------------------------------------------------------------- 参数解析
usage() { sed -n '4,/^set -u/p' "$0" | sed 's/^# \{0,1\}//;$d'; exit 0; }

while (( $# )); do
    case "$1" in
        -H|--host)     HOST="$2"; shift 2 ;;
        -p|--port)     PORT="$2"; shift 2 ;;
        -t|--topic)    TOPIC="$2"; shift 2 ;;
        -u|--username) MQTT_USER="$2"; shift 2 ;;
        -P|--password) MQTT_PASS="$2"; shift 2 ;;
        -c|--csv)      CSV="$2"; shift 2 ;;
        -g|--gain)     GAIN="$2"; shift 2 ;;
        -o|--offset)   OFFSET="$2"; shift 2 ;;
        -d|--duration) DURATION="$2"; shift 2 ;;
        --no-csv)      USE_CSV=0; shift ;;
        --no-db)       USE_DB=0; shift ;;
        --db-dsn)      DB_DSN="$2"; shift 2 ;;
        --model)       DB_MODEL="$2"; shift 2 ;;
        --device)      DB_MODEL="$2"; shift 2 ;;   # 旧名，兼容（09-26 起叫 --model）
        --i-lo)        I_LO="$2"; shift 2 ;;
        --i-hi)        I_HI="$2"; shift 2 ;;
        --c-lo)        C_LO="$2"; shift 2 ;;
        --c-hi)        C_HI="$2"; shift 2 ;;
        --client-id)   CLIENT_ID="$2"; shift 2 ;;
        --no-persist)  PERSIST=0; shift ;;
        -q|--qos)      QOS="$2"; shift 2 ;;
        --dedup)       DEDUP_SECONDS="$2"; shift 2 ;;
        --no-dedup)    DEDUP_SECONDS=0; shift ;;
        --pipe)        MODE="pipe"; shift ;;
        --replay)      MODE="replay"; REPLAY_FILE="$2"; shift 2 ;;
        -h|--help)     usage ;;
        *) echo "未知参数：$1（用 --help 看用法）" >&2; exit 2 ;;
    esac
done

# psql 不在（例如在本机 Mac 上试跑）⇒ 自动关掉入库并记下原因
if (( USE_DB )) && ! command -v psql >/dev/null 2>&1; then
    USE_DB=0; DB_WHY="未找到 psql，入库已自动关闭（在本机试跑属正常）"
fi

# ----------------------------------------------------------------- 小工具
# 十六进制串 → ASCII（用于识别心跳文本、注册包）
hex_to_ascii() {
    local h="$1" out="" c oct
    while (( ${#h} >= 2 )); do
        c="${h:0:2}"; h="${h:2}"
        printf -v oct '%03o' "$((16#$c))" 2>/dev/null || return 1
        out+="\\$oct"
    done
    printf '%b' "$out"
}

# 时间戳 → epoch 秒（GNU date 与 BSD date 都试一遍；失败则返回非 0）
ts_to_epoch() {
    local t="$1" e
    e="$(date -d "$t" +%s 2>/dev/null)" && [[ -n "$e" ]] && { echo "$e"; return 0; }
    e="$(date -j -f '%Y-%m-%dT%H:%M:%S%z' "$t" +%s 2>/dev/null)" && [[ -n "$e" ]] && { echo "$e"; return 0; }
    return 1
}

# 非数据帧的文字说明
describe_other() {
    local hex="$1" n=$(( ${#hex} / 2 )) asc
    if (( n == 16 )); then echo "注册包/设备ID"; return; fi
    if (( n <= 4 )); then
        asc="$(hex_to_ascii "$hex" 2>/dev/null)" || asc=""
        if [[ -n "$asc" && "$asc" =~ ^[[:print:]]+$ ]]; then
            echo "心跳/文本 “$asc”"; return
        fi
    fi
    echo "其它 ${n} 字节"
}

open_csv() {
    (( USE_CSV )) || return 0
    if [[ ! -s "$CSV" ]]; then
        echo '时间,raw,mA,浓度,未补偿,状态,原始payload' >> "$CSV"
    fi
}

# ----------------------------------------------------------------- CSV 行
csv_row() {   # $1 时间  $2 raw  $3 mA  $4 浓度  $5 带增益浓度  $6 状态  $7 payload
    (( USE_CSV )) || return 0
    printf '%s,%s,%s,%s,%s,%s,%s\n' "$1" "$2" "$3" "$4" "$5" "$6" "$7" >> "$CSV"
}

# ----------------------------------------------------------------- 数据库行
# ----------------------------------------------------------------- 数据库行
# ⭐ 只写三列：型号 / 时间 / 原始值。mA、浓度、状态一律由后端算（库里没有这些列）
#    口令走 /root/.pgpass（600）—— 不进命令行、不进环境变量、不进日志
db_row() {   # $1 时间(ISO8601)  $2 raw
    (( USE_DB )) || return 0
    local ts_sql="$1" out rc
    # mosquitto_sub -F %I 给的是 2026-09-25T23:26:36+0800；PG 更喜欢 +08:00
    if [[ "$ts_sql" =~ ^(.*[+-][0-9]{2})([0-9]{2})$ ]]; then
        ts_sql="${BASH_REMATCH[1]}:${BASH_REMATCH[2]}"
    fi
    # shellcheck disable=SC2086
    out="$(printf '%s\n' \
        "INSERT INTO raw_reading (model, ts, raw)
         VALUES ('${DB_MODEL}', '${ts_sql}', $2)
         ON CONFLICT DO NOTHING;" \
        | psql -X -q -w -v ON_ERROR_STOP=1 $DB_DSN 2>&1)"; rc=$?
    if (( rc == 0 )); then
        n_db_ok=$(( n_db_ok + 1 ))
    else
        n_db_err=$(( n_db_err + 1 ))
        if (( n_db_err <= 3 )); then
            echo "   ⚠️ 入库失败（第 ${n_db_err} 次）：$(printf '%s' "$out" | head -n 2 | tr '\n' ' ')"
        fi
    fi
    return 0
}

# ----------------------------------------------------------------- 处理一行
handle_line() {
    local line="$1" ts hex n b45 b7 b8 raw ma c cg st

    [[ "$line" == *"|"* ]] || return 0
    ts="${line%%|*}"
    hex="${line#*|}"
    # 去掉可能夹带的空格/引号/CR
    hex="$(printf '%s' "$hex" | tr -d ' \t\r\"' | tr 'A-F' 'a-f')"
    [[ "$hex" =~ ^[0-9a-f]+$ ]] || return 0

    [[ -n "$ts" ]] && { t_last="$ts"; [[ -z "$t_first" ]] && t_first="$ts"; }
    n=$(( ${#hex} / 2 ))

    # ---- 是不是 DTU 转出来的 Modbus TCP 数据帧？ ----
    raw=""
    if (( ${#hex} == 22 )); then
        b45="${hex:8:4}"; b7="${hex:14:2}"; b8="${hex:16:2}"
        if [[ "$b45" == "0005" && "$b7" == "04" && "$b8" == "02" ]]; then
            raw=$(( 16#${hex:18:4} ))
        fi
    fi

    # ---- QoS1 重传去重（09-26 加）----
    #   DTU 用 QoS1 上传：broker 未收到 PUBACK 会重发，而订阅端的接收时间不同 ⇒
    #   PK(model,ts,raw) 挡不住（ts 差几秒就是新行）。用「同 raw + 时间窗」判定重传。
    #   窗口 ${DEDUP_SECONDS}s 远小于上报节拍（约 100s），不会误杀真实的稳定读数。
    if [[ -n "$raw" && -n "$last_raw" && "$raw" == "$last_raw" && $DEDUP_SECONDS -gt 0 ]]; then
        local ep_dd
        if ep_dd="$(ts_to_epoch "$ts")" && [[ -n "$last_raw_ep" ]] \
           && (( ep_dd - last_raw_ep >= 0 && ep_dd - last_raw_ep < DEDUP_SECONDS )); then
            n_dup=$(( n_dup + 1 ))
            if (( n_dup <= 3 )); then
                printf '%-28s %-8s ↺ 与上一条重复（%ss 内同 raw，按 QoS1 重传丢弃，不入库）\n' \
                       "$ts" "$raw" "$(( ep_dd - last_raw_ep ))"
            fi
            return 0
        fi
    fi

    if [[ -z "$raw" ]]; then
        n_other=$(( n_other + 1 ))
        printf '%-28s · %s  (%d 字节, %s)\n' "$ts" "$(describe_other "$hex")" "$n" "$hex"
        # ⏳ 还没见到任何数据帧时，每分钟给一句进度提示
        if (( n_data == 0 && SECONDS - t_last_note >= 60 )); then
            t_last_note=$SECONDS
            echo "   ⏳ 已等待 ${SECONDS} 秒，收到 ${n_other} 条消息（心跳/注册包），但还没有数据帧"
            echo "      心跳每 120 秒一条 ⇒ 链路是活的；数据上传周期约 100 秒，请再耐心等几分钟（Ctrl-C 可随时收尾）"
        fi
        return 0
    fi

    # ---- 换算 ----
    read -r ma c cg <<< "$(awk -v r="$raw" -v ilo="$I_LO" -v ihi="$I_HI" \
                                 -v clo="$C_LO" -v chi="$C_HI" -v g="$GAIN" -v o="$OFFSET" \
        'BEGIN{ ma=r/1000; c=(ma-ilo)/(ihi-ilo)*(chi-clo)+clo;
                cg = c*g + o;                     # ⭐ 加性偏移（09-25 定案），不是 ×1.005
                printf "%.3f %.3f %.3f", ma, c, cg }')"

    if (( raw < BREAK_LOW )); then st="⚠️ 断线/仪表故障"
    elif (( raw > OVER_HIGH )); then st="⚠️ 超量程·不可信"
    else st="正常"; fi

    # ---- 上报间隔（数据帧之间的 Δt，用来量化「改参数有没有让上报变快」）----
    local dtxt="—" ep d
    if ep="$(ts_to_epoch "$ts")"; then
        last_raw="$raw"; last_raw_ep="$ep"      # 供下一次去重比对
        if [[ -n "$t_prev_data" ]]; then
            d=$(( ep - t_prev_data ))
            if (( d >= 0 )); then
                dtxt="${d}s"
                n_gap=$(( n_gap + 1 ))
                gap_sum=$(( gap_sum + d ))
                [[ -z "$gap_min" ]] && gap_min=$d
                [[ -z "$gap_max" ]] && gap_max=$d
                (( d < gap_min )) && gap_min=$d
                (( d > gap_max )) && gap_max=$d
            fi
        fi
        t_prev_data="$ep"
    fi

    n_data=$(( n_data + 1 ))
    # ⭐ 定案：「浓度」列 = 已经加过 +0.3 个点的最终值；另留「未补偿」列供溯源
    local cd="—" cgd="—" csv_c="" csv_cg=""
    if [[ "$st" == "正常" ]]; then
        n_ok=$(( n_ok + 1 ))
        # 统计一律基于【补偿后】的浓度（先生拿它对浓度计本体）
        csum="$(awk -v a="$csum" -v b="$cg" 'BEGIN{printf "%.3f", a+b}')"
        [[ -z "$cmin" ]] && cmin="$cg"
        cmax="$(awk -v a="$cmax" -v b="$cg" 'BEGIN{print (a<b)?b:a}')"
        awk -v a="$cmin" -v b="$cg" 'BEGIN{exit !(b<a)}' && cmin="$cg"
        cd="$cg"; cgd="$c"; csv_c="$cg"; csv_cg="$c"
    fi

    printf '%-28s %-8s %-9s %-9s %-9s %-6s %s\n' "$ts" "$raw" "$ma" "$cd" "$cgd" "$dtxt" "$st"

    # ⭐ 收到帧但值不正常时，大声喊出来（只喊前几次，避免刷屏）
    if [[ "$st" != "正常" ]]; then
        if (( raw == 0 )); then
            n_zero=$(( n_zero + 1 ))
            if (( n_zero <= 3 )); then
                echo "   🔴 模块报 0.000 mA —— 这不是「没数据」，是 4~20mA 回路断了或浓度计失电/没输出！"
                echo "      （4mA 才代表浓度 0；0mA 说明信号根本没上来 —— 活零的意义就在这）"
                echo "      👉 万用表 mA 档【串联】量回路电流 ／ 拔掉 DTU 的 A/B 后用中盛软件读全部 18 路，看是不是只有 CH1 为 0"
            fi
        else
            n_bad=$(( n_bad + 1 ))
            if (( n_bad <= 3 )); then
                echo "   ⚠️ 越限读数 raw=${raw} → ${ma} mA（${st}）—— 检查量程制式与接线"
            fi
        fi
    fi

    csv_row "$ts" "$raw" "$ma" "$csv_c" "$csv_cg" "$st" "$hex"
    db_row "$ts" "$raw"
}

# ----------------------------------------------------------------- 统计
print_summary() {
    echo
    echo "--------------------------------------------------------------------------------"
    if (( n_ok )); then
        echo "  数据帧 ${n_data} 个（正常 ${n_ok} ｜ 异常 $(( n_data - n_ok ))） ｜ 其它(注册包/心跳等) ${n_other} 条${n_dup:+ ｜ 去重丢弃 ${n_dup} 条}"
        printf '  浓度 最低 %s ｜ 最高 %s ｜ 平均 %s   （✅ 已补偿：加性 +%s 个点；乘性系数 x%s）\n' \
               "$cmin" "$cmax" "$(awk -v s="$csum" -v n="$n_ok" 'BEGIN{printf "%.3f", s/n}')" "$OFFSET" "$GAIN"
        echo "  区间  ${t_first}  →  ${t_last}"
        echo "  ⭐ 请把这段「浓度」与浓度计本体显示值对照 —— 它已经是补偿后的最终值"
    elif (( n_data )); then
        echo "  数据帧 ${n_data} 个（正常 0 ｜ 异常 ${n_data}） ｜ 其它(注册包/心跳等) ${n_other} 条"
        if (( n_zero )); then
            echo "  🔴 收到 ${n_zero} 个数据帧，但【全部是 0.000 mA】—— 通信链路完全正常，问题在模拟量侧："
            echo "     · 4~20mA 回路断线 / 接线松脱（模块端或浓度计端）"
            echo "     · 浓度计（变送器 TRA-9861）失电、关机、或没有输出"
            echo "     · 接错通道（CH1=0000H … CH18=0011H，读全部 18 路看哪一路有值）"
            echo "     👉 ① 万用表 mA 档【串联】进回路量电流（⚠️ 不可并联）"
            echo "        ② 拔掉 DTU 的 A/B（RS485 单主站），用中盛软件整块读 18 路"
        else
            echo "  ⚠️ 收到 ${n_data} 个数据帧，但没有一个落在正常区间（${n_bad} 个越限）" \
                 "—— 检查量程制式（0~20mA 还是 4~20mA）与接线"
        fi
    else
        echo "  数据帧 0 个（正常 0 ｜ 异常 0） ｜ 其它(注册包/心跳等) ${n_other} 条"
        echo "  ⚠️ 一个数据帧都没收到 —— 检查："
        echo "     0) 脚本有没有接上数据源（直接跑会自动拉起 mosquitto_sub；找不到它就用 --pipe）"
        echo "     1) 订阅是否挂在正确的主题上（当前：${TOPIC}）"
        echo "     2) DTU 的「推送主题1」是否填了 ${TOPIC}"
        echo "     3) DTU 的「主题1开关」是否选的是 qos=0/1/2（选「关闭推送」就是关）"
        echo "     4) DTU 南向轮询是否开着（采集开关 = 自定义轮询采集），指令 01040000000131CA"
        echo "     5) ⚠️ 上传周期约 100 秒（界面里没有这个参数，第一嫌疑是 +DTUPACKET: 50,2500）" \
             "—— 至少让脚本跑 3~5 分钟再看"
        echo "     6) ⭐ 心跳在但不来数据帧 ⇒ DTU 没拿到 485 数据：看 DTU 上 485 的 TX/RX 灯闪不闪"
    fi
    if (( n_gap )); then
        echo "  上报间隔 平均 $(( gap_sum / n_gap )) 秒（${n_gap} 个间隔 ｜ 最短 ${gap_min} ｜ 最长 ${gap_max} 秒）"
        echo "      ↑ 改完 DTU 参数后跑 5~10 分钟，看这个平均值有没有变小，就知道改对了没有"
    fi
    (( USE_CSV )) && echo "  记录已写入 ${CSV}"
    if (( USE_DB )); then
        if (( n_db_err )); then
            echo "  ⚠️ 数据库入库 成功 ${n_db_ok} 条 ｜ 失败 ${n_db_err} 条 —— 查 postgresql 服务与 /root/.pgpass"
        else
            echo "  ✅ 数据库入库 ${n_db_ok} 条 → raw_reading（型号 ${DB_MODEL}｜只存 型号/时间/原始值）"
        fi
    elif [[ -n "$DB_WHY" ]]; then
        echo "  数据库未启用（${DB_WHY}）"
    fi
    echo "--------------------------------------------------------------------------------"
}

finish() {
    (( SUMMARY_DONE )) && exit 0
    SUMMARY_DONE=1
    print_summary
    exit 0
}
trap finish INT TERM

# ----------------------------------------------------------------- 开跑
source_desc="mosquitto_sub 子进程 → ${HOST}:${PORT} 「${TOPIC}」"
if (( PERSIST )); then source_desc="${source_desc}（持久会话 client-id=${CLIENT_ID}，QoS ${QOS}）"; fi
if [[ -n "$MQTT_USER" ]]; then source_desc="${source_desc} 用户 ${MQTT_USER}"; else source_desc="${source_desc}（匿名）"; fi
case "$MODE" in
    pipe)   source_desc="stdin（管道）" ;;
    replay) source_desc="回放 ${REPLAY_FILE}" ;;
esac

GM_DESC="⭐ 浓度 = 未补偿 × ${GAIN} + ${OFFSET}　（09-25 定案：在换算出的浓度上【多加 $(awk -v o="$OFFSET" 'BEGIN{printf "%+.3f", o}') 个浓度点】，不是乘 1.005）"
[[ "$GAIN" == "1.0" || "$GAIN" == "1" ]] && GM_DESC="⭐ 浓度 = 未补偿 + ${OFFSET}　（09-25 定案：在换算出的浓度上【多加 ${OFFSET} 个浓度点】，不是乘 1.005）"

echo "================================================================================"
echo "  DPRO-66 浓度计实时监视                        ECAS / 2026-09-25"
echo "--------------------------------------------------------------------------------"
echo "  换算     未补偿 = (raw - $(awk -v x="$I_LO" 'BEGIN{printf "%d", x*1000}')) / $(awk -v a="$I_LO" -v b="$I_HI" -v c="$C_LO" -v d="$C_HI" 'BEGIN{printf "%.6g", 1000*(b-a)/(d-c)}')          <- ${I_LO}~${I_HI} mA  <->  ${C_LO}~${C_HI}"
echo "  浓度修正 ${GM_DESC}"
echo "  ⭐ 屏幕上的「浓度」列 = 已修正的最终值（可直接对浓度计）；「未补偿」列仅供溯源"
echo "  ⚠️ 断线判据 raw < ${BREAK_LOW}（<3.6mA） ｜ 超量程 raw > ${OVER_HIGH}（>20.5mA）"
echo "  数据来源 ${source_desc}"
(( USE_CSV )) && { case "$CSV" in /*) echo "  记录文件 ${CSV}";; *) echo "  记录文件 $(pwd)/${CSV}";; esac; }
if (( USE_DB )); then
    echo "  数据库    PostgreSQL raw_reading  ← 型号 ${DB_MODEL}（只存 型号/时间/原始值；换算在后端）"
elif [[ -n "$DB_WHY" ]]; then
    echo "  数据库    未启用（${DB_WHY}）"
fi
echo "--------------------------------------------------------------------------------"
echo "  👉 请在浓度计本体上读一下当前显示值，与下面的「浓度」列直接对照"
echo "================================================================================"
printf '%-28s %-8s %-9s %-9s %-9s %-6s %s\n' "时间" "raw" "mA" "浓度" "未补偿" "间隔" "状态"
echo "--------------------------------------------------------------------------------"

open_csv

ERRLOG="$(mktemp -t dpro66mon.XXXXXX)"
FIFO=""
SUBPID=""
cleanup_tmp() {
    if [[ -n "$SUBPID" ]]; then kill "$SUBPID" 2>/dev/null || true; fi
    if [[ -n "$FIFO" && -e "$FIFO" ]]; then rm -f "$FIFO"; fi
    if [[ -n "${ERRLOG:-}" && -f "$ERRLOG" ]]; then rm -f "$ERRLOG"; fi
    return 0
}
trap 'cleanup_tmp; finish' INT TERM

case "$MODE" in
    replay)
        if [[ ! -r "$REPLAY_FILE" ]]; then
            echo "❌ 读不到回放文件：${REPLAY_FILE}" >&2; exit 2
        fi
        while IFS= read -r line; do handle_line "$line"; done < "$REPLAY_FILE"
        ;;
    pipe)
        while IFS= read -r line; do handle_line "$line"; done
        ;;
    *)
        if ! command -v mosquitto_sub >/dev/null 2>&1; then
            echo "❌ 找不到 mosquitto_sub —— 请先安装，或改用管道方式：" >&2
            echo "   mosquitto_sub -h ${HOST} -p ${PORT} -t '${TOPIC}' ${MQTT_USER:+-u $MQTT_USER} ${MQTT_PASS:+-P <密码>} -F '%I|%x' | $0 --pipe" >&2
            cleanup_tmp; exit 2
        fi
        # ⭐ 命名管道 + 后台进程：既能拿到订阅进程 PID（收得干净），
        #    又能让 while 循环留在当前 shell（统计变量不会被吃掉）
        FIFO="$(mktemp -u -t dpro66fifo.XXXXXX)"
        mkfifo "$FIFO" || { echo "❌ 无法创建 FIFO：${FIFO}" >&2; exit 2; }

        if (( PERSIST )); then
            sub_i=(-i "$CLIENT_ID" -c)        # ⭐ 持久会话：离线期间 QoS1 帧不丢
        else
            sub_i=()                          # 旧行为：随机 ClientId + clean session
        fi
        mosquitto_sub -h "$HOST" -p "$PORT" -t "$TOPIC" -q "$QOS" -F '%I|%x' \
            "${sub_i[@]}" \
            ${MQTT_USER:+-u "$MQTT_USER"} ${MQTT_PASS:+-P "$MQTT_PASS"} \
            >"$FIFO" 2>"$ERRLOG" &
        SUBPID=$!
        exec 3< "$FIFO"          # 只打开一次，反复 read -u 3

        t_loop_start=$SECONDS
        while :; do
            if IFS= read -r -u 3 -t 1 line; then
                handle_line "$line"
            else
                rc=$?
                (( rc > 128 )) || break          # 1 = 管道关闭(EOF)；>128 = 本秒无数据
            fi
            if [[ -n "$DURATION" ]] && (( SECONDS - t_loop_start >= DURATION )); then
                echo "   ⏱️ 已到 ${DURATION} 秒，自动收尾"
                break
            fi
        done
        exec 3<&-

        if [[ -s "$ERRLOG" ]]; then
            echo
            echo "⚠️ mosquitto_sub 的输出：" >&2
            sed 's/^/   /' "$ERRLOG" >&2
        fi
        ;;
esac

cleanup_tmp
print_summary
exit 0
