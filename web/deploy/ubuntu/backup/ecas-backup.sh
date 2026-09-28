#!/usr/bin/env bash
# =============================================================================
# ECAS 本地栈 · 每日数据库备份（由 ecas-backup.timer 调用，也可手动跑）
#
# 产物：/data/ecas/backups/ecas-YYYY-MM-DD.dump（自定义格式，自带压缩）
#      同目录 .sha256 校验文件；日志进 journal：journalctl -u ecas-backup
# 保留：30 天（KEEP_DAYS 可覆盖）
#
# 为什么需要它：2026-09-26 起腾讯云只做中转（easytier + mosquitto），
#   数据库唯一副本在本机 /data/ecas/pgdata，备份责任随之搬到本机。
#
# 手动跑：/opt/ecas/backup/ecas-backup.sh
# 恢复演练（另起一个空库验一遍，不动生产库）：
#   docker compose exec -T db createdb -U ecas ecas_restore_drill
#   docker compose exec -T db pg_restore -U ecas -d ecas_restore_drill < 备份文件
#   docker compose exec -T db psql -U ecas -d ecas_restore_drill -c 'select count(*) from raw_reading;'
#   docker compose exec -T db dropdb -U ecas ecas_restore_drill
# =============================================================================
set -euo pipefail

STACK_DIR=${STACK_DIR:-/opt/ecas}
DEST=${DEST:-/data/ecas/backups}
KEEP_DAYS=${KEEP_DAYS:-30}

cd "$STACK_DIR"
install -d -m 700 "$DEST"

stamp=$(date +%Y-%m-%d)
part="$DEST/.ecas-$stamp.dump.part"
final="$DEST/ecas-$stamp.dump"

# 先写临时文件、校验通过再就位：绝不拿半截 dump 覆盖掉上一份好备份
docker compose exec -T db pg_dump -Fc -U ecas -d ecas > "$part"

# 校验：能把归档目录列出来，才说明这份 dump 是可恢复的
# （pg_restore 不给文件名时读标准输入；给 "-" 会被当成文件名，报 No such file）
docker compose exec -T db pg_restore -l < "$part" > /dev/null

mv "$part" "$final"
( cd "$DEST" && sha256sum "$(basename "$final")" > "$(basename "$final").sha256" )

# 轮转
find "$DEST" -maxdepth 1 -type f -name 'ecas-*.dump' -mtime +"$KEEP_DAYS" -delete
find "$DEST" -maxdepth 1 -type f -name 'ecas-*.dump.sha256' -mtime +"$KEEP_DAYS" -delete

size=$(stat -c%s "$final")
rows=$(docker compose exec -T db psql -U ecas -d ecas -Atc 'select count(*) from public.raw_reading')
echo "✅ 备份完成 $final"
echo "   $(numfmt --to=iec "$size" 2>/dev/null || echo "${size}B") ｜ 线上 $rows 行 ｜ sha256 $(cut -c1-16 "$final.sha256")…"
echo "   现有备份 $(find "$DEST" -maxdepth 1 -type f -name 'ecas-*.dump' | wc -l) 份（保留 ${KEEP_DAYS} 天）"
