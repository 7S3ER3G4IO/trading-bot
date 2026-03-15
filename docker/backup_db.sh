#!/bin/bash
# backup_db.sh — Phase 1.3: Automated PostgreSQL backup
# Run via cron or Docker exec daily at 03h UTC
#
# Usage:
#   docker exec nemesis_postgres /app/backup_db.sh
#   OR: add to docker-compose healthcheck as periodic job

set -e

BACKUP_DIR="/app/data/backups"
RETENTION_DAYS=7
DB_NAME="${POSTGRES_DB:-nemesis}"
DB_USER="${POSTGRES_USER:-nemesis}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/nemesis_${TIMESTAMP}.sql.gz"

# Create backup directory
mkdir -p "$BACKUP_DIR"

# Dump + compress
echo "📦 Starting backup: ${BACKUP_FILE}"
pg_dump -U "$DB_USER" "$DB_NAME" | gzip > "$BACKUP_FILE"
BACKUP_SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
echo "✅ Backup complete: ${BACKUP_FILE} (${BACKUP_SIZE})"

# Rotate old backups (keep last RETENTION_DAYS)
find "$BACKUP_DIR" -name "nemesis_*.sql.gz" -mtime +${RETENTION_DAYS} -delete
REMAINING=$(ls -1 "$BACKUP_DIR"/nemesis_*.sql.gz 2>/dev/null | wc -l)
echo "🗂️  Backups: ${REMAINING} files (retention: ${RETENTION_DAYS} days)"
