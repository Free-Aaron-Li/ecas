#!/usr/bin/env bash
# ============================================================================
# ECAS 每日备份：pg_dump -Fc → 校验 → 30 天轮转
#
#   ecas_pgdump.sh                 正常备份（由 ecas-pgdump.timer 每夜调用）
#   ecas_pgdump.sh --restore-drill 恢复演练：把最新 dump 真恢复到一个临时库，
#                                  比对行数后删库（证明备份是**能还原的**）
#
# 产物：/root/ecas/archive/db/ecas-YYYY-MM-DD.dump（自定义格式，自带压缩）
#      同目录 .sha256 校验文件；日志 /root/ecas/logs/pgdump.log
# ⚠️ 口令走 /root/.pgpass，脚本里没有口令。
# ============================================================================
set -uo pipefail

export PGPASSFILE=/root/.pgpass      # systemd 下 HOME 不一定有，显式指定

DEST=/root/ecas/archive/db
LOG=/root/ecas/logs/pgdump.log
KEEP_DAYS=30
DRILL_DB=ecas_restore_drill

mkdir -p "$DEST" "$(dirname "$LOG")"
log() { printf '%s  %s\n' "$(date '+%F %T')" "$*" >>"$LOG"; printf '%s  %s\n' "$(date '+%F %T')" "$*"; }

live_rows() {
    psql -X -qtAw -h 127.0.0.1 -U ecas -d ecas -c 'SELECT count(*) FROM raw_reading;' 2>/dev/null
}

# ── 恢复演练 ─────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--restore-drill" ]]; then
    f="$(ls -1t "$DEST"/ecas-*.dump 2>/dev/null | head -1)"
    [[ -n "$f" ]] || { log "❌ 没有可用的 dump"; exit 1; }
    log "🩺 恢复演练开始：$f"
    # ⚠️ 不能以 postgres 身份直接读 /root 下的 dump（/root 是 700，postgres 进不去）
    #    ⇒ 用 postgres 建库，再用 ecas（走 /root/.pgpass）恢复进去
    su - postgres -c "dropdb --if-exists $DRILL_DB" >/dev/null 2>&1
    if ! su - postgres -c "createdb -O ecas $DRILL_DB" >/dev/null 2>&1; then
        log "❌ 建临时库失败（$DRILL_DB）"; exit 1
    fi
    if ! err=$(pg_restore -h 127.0.0.1 -U ecas -d $DRILL_DB "$f" 2>&1); then
        log "❌ pg_restore 失败 —— 备份不可还原！"
        printf '%s\n' "$err" | sed 's/^/    /' | head -10 >>"$LOG"
        su - postgres -c "dropdb --if-exists $DRILL_DB" >/dev/null 2>&1
        exit 1
    fi
    d=$(psql -X -qtAw -h 127.0.0.1 -U ecas -d $DRILL_DB -c 'SELECT count(*) FROM raw_reading;' 2>/dev/null | tr -d '[:space:]')
    su - postgres -c "dropdb $DRILL_DB" >/dev/null 2>&1
    l="$(live_rows)"
    if [[ "$d" == "$l" ]]; then
        log "✅ 恢复演练通过：dump 里 $d 行 = 线上 $l 行"
        exit 0
    fi
    log "⚠️ 恢复演练行数不一致：dump $d 行 vs 线上 $l 行（若线上刚进过新数据，属正常的时间差）"
    exit 0
fi

# ── 正常备份 ─────────────────────────────────────────────────────────────
ts=$(date '+%F')
f="$DEST/ecas-$ts.dump"

if ! pg_dump -h 127.0.0.1 -U ecas -d ecas -Fc -f "$f" 2>>"$LOG"; then
    log "❌ pg_dump 失败（详见本行上方 pg_dump 的错误输出）"
    rm -f "$f"
    exit 1
fi

# 校验 1：目录能列出来（半截文件在这里就会报错）
if ! pg_restore -l "$f" >/dev/null 2>>"$LOG"; then
    log "❌ 备份文件损坏（pg_restore -l 失败）：$f"
    exit 1
fi

# 校验 2：sha256 存档 + 与线上行数比对
sha256sum "$f" >"$f.sha256" 2>/dev/null
size=$(stat -c %s "$f")
sha=$(cut -c1-16 <"$f.sha256")
log "✅ 备份成功 $(basename "$f")（${size} 字节，sha256 ${sha}…，线上 $(live_rows) 行）"

# ── 轮转：保留最近 KEEP_DAYS 天 ──────────────────────────────────────────
old=$(find "$DEST" -name 'ecas-*.dump' -mtime "+$KEEP_DAYS" -print -delete 2>/dev/null | wc -l)
find "$DEST" -name 'ecas-*.dump.sha256' -mtime "+$KEEP_DAYS" -delete 2>/dev/null
log "🧹 轮转完成：删除 $old 份超期备份，现有 $(find "$DEST" -name 'ecas-*.dump' 2>/dev/null | wc -l) 份（保留 $KEEP_DAYS 天）"
