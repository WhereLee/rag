#!/bin/bash
# 每日数据库自动备份：pg_dump 全量 → 压缩 → coscmd 上传 COS → 本地/COS 双保留策略
# 密钥来源：~/.cos.conf（coscmd config 生成，权限 600，不进 git）
# 用法：bash backup_cos.sh [--now]   （--now=立即执行一次，忽略 crontab 时间窗口）
set -euo pipefail

# ---------- 配置 ----------
BACKUP_DIR=/opt/rag/backups
PG_HOST=127.0.0.1
PG_USER=rag_app
PG_DB=rag_kb
COS_PREFIX=rag-backup          # COS 上的目录前缀
LOCAL_KEEP_DAYS=7              # 本地保留天数
COS_KEEP_DAYS=30               # COS 保留天数
COSCMD=/opt/rag/coscmd-venv/bin/coscmd
VENV_PY=/opt/rag/rag-python/.venv/bin/python
LOG=/opt/rag/backups/backup.log

mkdir -p "$BACKUP_DIR"
TS=$(date +%Y%m%d_%H%M%S)
STAMP="[$(date '+%Y-%m-%d %H:%M:%S')]"

log() { echo "$STAMP $*" >> "$LOG"; echo "$STAMP $*"; }

# ---------- 前置检查：coscmd 已配置 ----------
if [ ! -f ~/.cos.conf ]; then
  log "ERROR: ~/.cos.conf 不存在——先执行 coscmd config -a <SecretId> -s <SecretKey> -b <Bucket-APPID> -r <Region>"
  exit 1
fi

# ---------- 1/4 数据库全量 dump + 压缩 ----------
PW=$(sed -n 's|.*rag_app:\([^@]*\)@.*|\1|p' /opt/rag/.env)
export PGPASSWORD="$PW"
DUMP="$BACKUP_DIR/rag_kb_${TS}.dump"
log "1/4 pg_dump 开始 ($PG_DB)"
if ! pg_dump -h "$PG_HOST" -U "$PG_USER" -d "$PG_DB" -Fc -f "$DUMP" 2>>"$LOG"; then
  log "ERROR: pg_dump 失败，终止"
  rm -f "$DUMP"
  exit 1
fi
gzip -f "$DUMP"
GZ="$DUMP.gz"
SZ=$(du -h "$GZ" | cut -f1)
log "dump 完成: $GZ ($SZ)"

# ---------- 2/4 顺带备份 .env（恢复环境必需，不进 git） ----------
cp /opt/rag/.env "$BACKUP_DIR/env_${TS}.bak"
log "2/4 .env 已备份"

# ---------- 3/4 上传 COS ----------
COS_PATH="$COS_PREFIX/rag_kb_${TS}.dump.gz"
log "3/4 上传 COS: $COS_PATH"
if ! "$COSCMD" upload "$GZ" "$COS_PATH" >> "$LOG" 2>&1; then
  log "ERROR: COS 上传失败（本地备份保留，可手动重传）"
else
  log "上传成功: $COS_PATH"
fi

# ---------- 4/4 双端保留策略清理 ----------
log "4/4 清理过期备份（本地 ${LOCAL_KEEP_DAYS} 天 / COS ${COS_KEEP_DAYS} 天）"
find "$BACKUP_DIR" -name 'rag_kb_*.dump.gz' -mtime "+$LOCAL_KEEP_DAYS" -delete
find "$BACKUP_DIR" -name 'env_*.bak' -mtime "+$LOCAL_KEEP_DAYS" -delete

# COS 端：列出 rag-backup/ 下文件，删 30 天前的（coscmd list 输出格式：路径 大小 存储类型 时间）
"$COSCMD" list "$COS_PREFIX/" 2>/dev/null | while read -r cpath csize ctype ctime; do
  if [ -n "$cpath" ]; then
    fdate=$(echo "$cpath" | grep -oE '[0-9]{8}' || true)
    if [ -n "$fdate" ]; then
      age_days=$(( ( $(date +%s) - $(date -d "${fdate:0:4}-${fdate:4:2}-${fdate:6:2}" +%s) ) / 86400 ))
      if [ "$age_days" -gt "$COS_KEEP_DAYS" ]; then
        "$COSCMD" delete -f "$cpath" >> "$LOG" 2>&1 && log "COS 清理: $cpath (${age_days}天)"
      fi
    fi
  fi
done

log "备份完成 ✔（本地保留 $LOCAL_KEEP_DAYS 天 / COS 保留 $COS_KEEP_DAYS 天）"
echo "== DONE =="
