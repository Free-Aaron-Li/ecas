#!/usr/bin/env bash
# =============================================================================
# ECAS 本地看门狗（由 ecas-watchdog.timer 每 5 分钟调用，也可手动跑）
#
# 为什么在本地：2026-09-26 起云端只做中转，数据库、采集、后端、看板全在这台机，
#   云端那套 ecas-healthcheck.sh 依赖云库、已随退库停用 ⇒ 巡检责任搬到这里。
#
# 盯十件事（每一件都是「只有这台机能发现、且会让数据悄悄断掉」的故障）：
#   1. db        数据库可达（容器在跑 + pg_isready + 能查表）
#   2. fresh     数据新鲜度 —— 最新一条 raw_reading 距今超过 STALE_MIN 分钟（默认 10）
#   2b conc      浓度计专用新鲜度（raw_reading, model=DPRO-66）—— 2026-11-10 断流事故后新增
#   2c flow      流量计专用新鲜度（flow_reading）—— 页面「部分正常」曾掩盖浓度断流
#   3. collector 采集器 / 后端 / 数据库三容器状态（含重启次数暴涨 = 崩溃循环）
#   3b broker    mosquitto 容器 + 1883/8884 监听（2026-11-10 配置文件丢失导致 broker 起不来）
#   3c rtu       rtu-master 容器 + /opt/ecas/rtu-master/logs/rtu-live.jsonl 新鲜度
#   3d config    /opt/ecas/mqtt 三份配置齐全（缺失即从 backup 或凭据自愈）
#   4. api       本机看板 /healthz 是否 200
#   5. backup    每日备份是否按时产出（最新 dump 超过 BACKUP_MAX_HOURS 小时未更新）
#   6. disk      /data 与 / 余量（写满 = 全栈停摆）
#
# 持续异常会每 REPEAT_SECS 秒再播一次「仍未恢复」提醒（2026-11-10：只播一次的 crit
#   会被 Mac 侧去重后彻底静音 ⇒ 3 小时断流无人知道）。
#
# 产物：
#   /var/log/ecas/watchdog.jsonl   每次「状态变化」一行（正常→异常、异常→恢复）
#   /var/lib/ecas/watchdog.state   当前各项状态（用于比对，避免每 5 分钟刷屏）
#   public.health_event           同上事件入本地库（库不可达时只落文件，绝不因此失败）
#   journald                       每次运行的汇总：journalctl -u ecas-watchdog
#
# 通知钩子（可选）：/opt/ecas/.env 里设 WATCHDOG_NOTIFY_CMD=/path/to/脚本
#   状态发生变化时会把一行中文消息作为**唯一参数**传给它。没设 = 只记录不打扰。
#
# 退出码恒为 0（异常通过 journal/文件/库 呈现，不把 unit 打成 failed —— 现场 DTU
#   夜间停发属正常，unit failed 会淹没真故障）；内部错误才非 0。
# =============================================================================
set -uo pipefail

STACK_DIR=${STACK_DIR:-/opt/ecas}
STATE_DIR=${STATE_DIR:-/var/lib/ecas}
LOG_DIR=${LOG_DIR:-/var/log/ecas}
STALE_MIN=${STALE_MIN:-10}                 # 多久没新数据算断流（现场节拍 100s）
CONC_STALE_MIN=${CONC_STALE_MIN:-5}        # 浓度计被动帧约 20s 一条 ⇒ 5 分钟没新值即异常
FLOW_STALE_MIN=${FLOW_STALE_MIN:-10}       # 流量计约 6 行/分
RTU_STALE_SEC=${RTU_STALE_SEC:-180}        # rtu-live.jsonl 最新写入超过 3 分钟即异常
REPEAT_SECS=${REPEAT_SECS:-1800}           # 持续异常每 30 分钟再提醒一次
BACKUP_MAX_HOURS=${BACKUP_MAX_HOURS:-26}   # 备份最长可多少小时未更新
DISK_WARN=${DISK_WARN:-85}
DISK_CRIT=${DISK_CRIT:-92}
MQTT_DIR=${MQTT_DIR:-/opt/ecas/mqtt}
SELFHEAL="$STACK_DIR/watchdog/ecas-mqtt-selfheal.sh"

mkdir -p "$STATE_DIR" "$LOG_DIR"
STATE_FILE="$STATE_DIR/watchdog.state"
JSONL="$LOG_DIR/watchdog.jsonl"
cd "$STACK_DIR" || { echo "❌ 找不到 $STACK_DIR" >&2; exit 2; }

# 从 .env 取绑定地址/端口（root 可读；取不到就用默认）
env_get() { sed -n "s/^$1=//p" "$STACK_DIR/.env" 2>/dev/null | tail -1; }
BIND_IP=$(env_get BIND_IP); BIND_IP=${BIND_IP:-127.0.0.1}   # 2026-10-26 起默认回环
WEB_PORT=$(env_get ECAS_WEB_PORT); WEB_PORT=${WEB_PORT:-18080}
NOTIFY_CMD=$(env_get WATCHDOG_NOTIFY_CMD)

psql_q() { docker compose exec -T db psql -U ecas -d ecas -Atc "$1" < /dev/null 2>/dev/null; }

declare -a NAMES=() STATUS=() DETAIL=()
add() { NAMES+=("$1"); STATUS+=("$2"); DETAIL+=("$3"); }

# ── 3. 容器状态（先查它，后面几项才有意义）────────────────────────────────────
containers=$(docker compose ps --format '{{.Service}}|{{.State}}|{{.Health}}' 2>/dev/null)
c_bad=""; c_detail=""
for svc in db api collector; do
    line=$(printf '%s\n' "$containers" | grep "^$svc|" || true)
    st=$(printf '%s' "$line" | cut -d'|' -f2); hl=$(printf '%s' "$line" | cut -d'|' -f3)
    [[ -z "$line" ]] && { c_bad=1; c_detail+="$svc 未运行; "; continue; }
    [[ "$st" != "running" ]] && { c_bad=1; c_detail+="$svc=$st; "; }
    [[ "$hl" == "unhealthy" ]] && { c_bad=1; c_detail+="$svc unhealthy; "; }
done
# 崩溃循环：最近 15 分钟内重启次数 > 3
for svc in api collector; do
    cid=$(docker compose ps -q "$svc" 2>/dev/null); [[ -z "$cid" ]] && continue
    rc=$(docker inspect -f '{{.RestartCount}}' "$cid" 2>/dev/null || echo 0)
    started=$(docker inspect -f '{{.State.StartedAt}}' "$cid" 2>/dev/null || true)
    if [[ "${rc:-0}" -gt 3 && -n "$started" ]]; then
        age=$(( $(date +%s) - $(date -d "$started" +%s 2>/dev/null || echo 0) ))
        [[ $age -lt 900 ]] && { c_bad=1; c_detail+="$svc 15 分钟内重启 ${rc} 次; "; }
    fi
done
if [[ -n "$c_bad" ]]; then add collector crit "容器异常：${c_detail%; }"
else add collector ok "db/api/collector 三容器 running"; fi

# ── 3d. MQTT 配置自愈（2026-11-10：三份配置丢失 ⇒ broker 退出码 127）─────────
cfg_bad=""
for f in mosquitto.conf passwd acl.conf; do
    [[ -f "$MQTT_DIR/$f" ]] || cfg_bad+="$f 缺失; "
done
if [[ -n "$cfg_bad" ]]; then
    heal_msg="配置缺失（${cfg_bad%; }）"
    if [[ -x "$SELFHEAL" ]]; then
        out=$(bash "$SELFHEAL" 2>&1); hrc=$?
        if [[ $hrc -eq 0 ]]; then
            add config warn "${heal_msg}，已由 ecas-mqtt-selfheal.sh 自愈"
        else
            add config crit "${heal_msg}，自愈失败（rc=${hrc}）：$(printf '%s' "$out" | tail -2 | tr '\n' ' ')"
        fi
    else
        add config crit "${heal_msg}，且找不到自愈脚本 $SELFHEAL"
    fi
else
    add config ok "mosquitto.conf / passwd / acl.conf 齐全"
fi

# ── 3b. broker（非 compose 容器，2026-11-10 起纳入）──────────────────────────
b_bad=""
bst=$(docker inspect -f '{{.State.Status}}' ecas-mosquitto 2>/dev/null || echo missing)
[[ "$bst" != "running" ]] && b_bad+="容器=$bst; "
ss -ltn 2>/dev/null | grep -qE ':1883[[:space:]]' || b_bad+="1883 未监听; "
ss -ltn 2>/dev/null | grep -qE ':8884[[:space:]]' || b_bad+="8884 未监听; "
if [[ -n "$b_bad" ]]; then add broker crit "MQTT broker 异常：${b_bad%; }"
else add broker ok "ecas-mosquitto running，1883/8884 在监听"; fi

# ── 3c. rtu-master（非 compose 容器）─────────────────────────────────────────
r_bad=""
rst=$(docker inspect -f '{{.State.Status}}' ecas-rtu-master 2>/dev/null || echo missing)
[[ "$rst" != "running" ]] && r_bad+="容器=$rst; "
RTU_LOG="/opt/ecas/rtu-master/logs/rtu-live.jsonl"
if [[ -f "$RTU_LOG" ]]; then
    rage=$(( $(date +%s) - $(stat -c %Y "$RTU_LOG" 2>/dev/null || echo 0) ))
    [[ $rage -gt $RTU_STALE_SEC ]] && r_bad+="rtu-live.jsonl ${rage}s 未更新; "
else
    r_bad+="rtu-live.jsonl 不存在; "
fi
if [[ -n "$r_bad" ]]; then add rtu crit "采集主站异常：${r_bad%; }"
else add rtu ok "ecas-rtu-master running，rtu-live.jsonl 新鲜"; fi

# ── 1. 数据库可达 ────────────────────────────────────────────────────────────
db_ok=0
if docker compose exec -T db pg_isready -U ecas -d ecas < /dev/null >/dev/null 2>&1; then
    rows=$(psql_q 'select count(*) from public.raw_reading')
    if [[ -n "${rows:-}" ]]; then add db ok "raw_reading ${rows} 行"; db_ok=1
    else add db crit "pg_isready 通过，但查 raw_reading 失败"; fi
else
    add db crit "数据库不可达（容器的 pg_isready 不通过）"
fi

# ── 2. 数据新鲜度 ────────────────────────────────────────────────────────────
if [[ $db_ok == 1 ]]; then
    # psql -Atc 的多列用 "|" 分隔，read 的 IFS 必须显式指定，否则整行都塞进第一个变量
    IFS='|' read -r age_s last_ts < <(psql_q "select coalesce(extract(epoch from now()-max(ts))::int,-1), coalesce(to_char(max(ts),'YYYY-MM-DD HH24:MI:SS'),'无数据') from public.raw_reading")
    age_s=${age_s:-}
    last_ts=${last_ts:-无数据}
    case "$age_s" in ''|*[!0-9]*) age_ok=0 ;; *) age_ok=1 ;; esac   # 非纯数字（含负值/空）= 没有数据
    if [[ $age_ok == 0 ]]; then
        add fresh crit "库里一条数据都没有（或时间戳解析失败）"
    else
        mins=$(( age_s / 60 ))
        if [[ $age_s -gt $(( STALE_MIN * 60 )) ]]; then
            add fresh crit "最新数据 ${mins} 分钟前（$last_ts），超过 ${STALE_MIN} 分钟阈值"
        else
            add fresh ok "最新数据 $last_ts（${mins} 分钟前）"
        fi
    fi
else
    add fresh crit "数据库不可达，无法判断数据新鲜度"
fi

# ── 2b/2c. 分传感器新鲜度（2026-11-10：flow 正常掩盖了 conc 断流 3 小时）──────
if [[ $db_ok == 1 ]]; then
    for spec in "conc:raw_reading:model='DPRO-66':$CONC_STALE_MIN" \
                "flow:flow_reading:1=1:$FLOW_STALE_MIN"; do
        IFS=':' read -r nm tbl cond thr <<<"$spec"
        sage=$(psql_q "select coalesce(extract(epoch from now()-max(ts))::int,-1) from public.$tbl where $cond")
        case "$sage" in ''|*[!0-9]*) add "$nm" crit "$nm 无可用数据（查询失败或表为空）"; continue ;; esac
        if [[ $sage -gt $(( thr * 60 )) ]]; then
            add "$nm" crit "$nm 最新数据 $(( sage / 60 )) 分钟前（阈值 ${thr} 分钟）"
        else
            add "$nm" ok "$nm 最新数据 $(( sage / 60 )) 分钟前"
        fi
    done
else
    add conc crit "数据库不可达，无法判断浓度数据新鲜度"
    add flow crit "数据库不可达，无法判断流量数据新鲜度"
fi

# ── 4. 看板健康 ──────────────────────────────────────────────────────────────
code=$(curl -s -o /dev/null -m 10 -w '%{http_code}' "http://$BIND_IP:$WEB_PORT/healthz" 2>/dev/null || echo 000)
if [[ "$code" == "200" ]]; then add api ok "http://$BIND_IP:$WEB_PORT/healthz → 200"
else add api crit "/healthz 返回 $code（期望 200）"; fi

# ── 5. 备份新鲜度 ────────────────────────────────────────────────────────────
if [[ ! -r /data/ecas/backups ]]; then
    # 目录 700 root：非 root 手动跑会读不到（2026-11-10 起不再误报"没有备份"）
    add backup ok "备份目录不可读（当前非 root，跳过；systemd 以 root 跑）"
    newest=""
else
    newest=$(ls -1t /data/ecas/backups/ecas-*.dump 2>/dev/null | head -1)
fi
if [[ -r /data/ecas/backups ]] && [[ -z "$newest" ]]; then
    add backup warn "没有任何备份文件（/data/ecas/backups/ 为空）"
elif [[ -n "$newest" ]]; then
    bh=$(( ( $(date +%s) - $(stat -c %Y "$newest") ) / 3600 ))
    if [[ $bh -gt $BACKUP_MAX_HOURS ]]; then
        add backup crit "最新备份 $(basename "$newest") 已 ${bh} 小时未更新（阈值 ${BACKUP_MAX_HOURS}h）"
    else
        add backup ok "最新备份 $(basename "$newest")（${bh} 小时前）"
    fi
fi

# ── 6. 磁盘 ──────────────────────────────────────────────────────────────────
disk_detail=""; disk_st=ok
for mnt in /data /; do
    pct=$(df --output=pcent "$mnt" 2>/dev/null | tail -1 | tr -dc '0-9')
    [[ -z "$pct" ]] && continue
    disk_detail+="$mnt ${pct}%；"
    if [[ $pct -ge $DISK_CRIT ]]; then disk_st=crit
    elif [[ $pct -ge $DISK_WARN && $disk_st == ok ]]; then disk_st=warn; fi
done
add disk "$disk_st" "${disk_detail%;}"

# ── 状态比对 → 只在「变化」时落事件，避免每 5 分钟刷屏 ────────────────────────
now_iso=$(date -Is)
prev=""; had_state=0
if [[ -f "$STATE_FILE" ]]; then prev=$(cat "$STATE_FILE"); had_state=1; fi
new_state=""; events=()
declare -A changed=()
for i in "${!NAMES[@]}"; do
    n=${NAMES[$i]}; s=${STATUS[$i]}
    new_state+="$n=$s"$'\n'
    old=$(printf '%s\n' "$prev" | sed -n "s/^$n=//p" | tail -1)
    if [[ "$old" != "$s" ]]; then
        changed["$n"]=1
        if [[ "$s" == "ok" ]]; then kind=cleared; else kind=problem; fi
        [[ $had_state == 1 ]] && events+=("$n|$s|$kind|${DETAIL[$i]}")
    fi
done
printf '%s' "$new_state" > "$STATE_FILE"

# ── 持续异常再提醒（2026-11-10）──────────────────────────────────────────────
# 原来的实现只在「状态变化」时落一行；Mac 侧按内容去重 ⇒ 一条 crit 播过一次就永久静音，
#   11-10 浓度断流 3 小时无人知晓。这里对仍未恢复的检查项每 REPEAT_SECS 重播一次。
NOTIFY_STATE="$STATE_DIR/watchdog.notified"
declare -A last_notified=()
if [[ -f "$NOTIFY_STATE" ]]; then
    while IFS='=' read -r k v; do [[ -n "$k" ]] && last_notified["$k"]="${v:-0}"; done < "$NOTIFY_STATE"
fi
ns_now=$(date +%s)
for i in "${!NAMES[@]}"; do
    n=${NAMES[$i]}; s=${STATUS[$i]}
    if [[ "$s" == "ok" ]]; then last_notified["$n"]=0; continue; fi
    [[ -n "${changed[$n]:-}" ]] && { last_notified["$n"]=$ns_now; continue; }
    last=${last_notified[$n]:-0}
    case "$last" in ''|*[!0-9]*) last=0 ;; esac
    if (( last == 0 )) || (( ns_now - last >= REPEAT_SECS )); then
        [[ $had_state == 1 ]] && events+=("$n|$s|problem|${DETAIL[$i]}（仍未恢复：距上次提醒约 $(( last == 0 ? 0 : (ns_now - last) / 60 )) 分钟）")
        last_notified["$n"]=$ns_now
    fi
done
: > "$NOTIFY_STATE"
for k in "${!last_notified[@]}"; do printf '%s=%s\n' "$k" "${last_notified[$k]}" >> "$NOTIFY_STATE"; done
[[ $had_state == 1 ]] || echo "  （首次运行：建立状态基线，本次不落事件）"

if (( ${#events[@]} > 0 )); then
    psql_q "CREATE TABLE IF NOT EXISTS public.health_event (
              id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(),
              check_name text NOT NULL, status text NOT NULL, kind text NOT NULL, detail text NOT NULL DEFAULT '');" >/dev/null
    for e in "${events[@]}"; do
        IFS='|' read -r n s kind detail <<<"$e"
        safe_detail=${detail//\"/\'}; safe_detail=${safe_detail//\\/}
        printf '{"ts":"%s","check":"%s","status":"%s","event":"%s","detail":"%s"}\n' \
            "$now_iso" "$n" "$s" "$kind" "$safe_detail" >> "$JSONL"
        psql_q "INSERT INTO public.health_event (check_name,status,kind,detail) VALUES ('$n','$s','$kind','$safe_detail');" >/dev/null
        msg="[ECAS 看门狗] $now_iso $n → $s（${kind}）：$safe_detail"
        echo "$msg"
        if [[ -n "$NOTIFY_CMD" && -x "$NOTIFY_CMD" ]]; then "$NOTIFY_CMD" "$msg" >/dev/null 2>&1 || true; fi
    done
fi

# ── 汇总（每次运行都打印，进 journald）───────────────────────────────────────
icon() { case "$1" in ok) echo ✅ ;; warn) echo ⚠️ ;; *) echo ❌ ;; esac; }
n_ok=0; n_warn=0; n_crit=0
echo "── ECAS 看门狗 $now_iso ──"
for i in "${!NAMES[@]}"; do
    printf '  %s %-10s %s\n' "$(icon "${STATUS[$i]}")" "${NAMES[$i]}" "${DETAIL[$i]}"
    case "${STATUS[$i]}" in ok) n_ok=$((n_ok+1)) ;; warn) n_warn=$((n_warn+1)) ;; *) n_crit=$((n_crit+1)) ;; esac
done
echo "  合计 ${#NAMES[@]} 项：正常 $n_ok ｜ 警告 $n_warn ｜ 严重 $n_crit（事件流水 $JSONL）"
n_crit=$(( n_crit + n_warn * 0 + 0 ))
if (( n_crit > 0 )); then
    echo "  最近事件："; tail -3 "$JSONL" 2>/dev/null | sed 's/^/    /'
fi
exit 0
