#!/usr/bin/env bash
# disk_monitor.sh — Alerte Discord si disque VPS > 85%
# Crontab : */30 * * * * /opt/bot/disk_monitor.sh

DISCORD_WEBHOOK="${DISCORD_MONITORING_WEBHOOK:-}"
THRESHOLD=85

usage=$(df / | awk 'NR==2 {gsub(/%/,""); print $5}')

if [ -z "$usage" ]; then
  exit 0
fi

if [ "$usage" -ge "$THRESHOLD" ]; then
  hostname_val=$(hostname 2>/dev/null || echo "VPS")
  message="⚠️ **DISK ALERT** \`${hostname_val}\` — Disque à **${usage}%** (seuil=${THRESHOLD}%)"
  
  if [ -n "$DISCORD_WEBHOOK" ]; then
    curl -s -X POST "$DISCORD_WEBHOOK" \
      -H "Content-Type: application/json" \
      -d "{\"content\": \"${message}\"}" \
      >/dev/null 2>&1
  fi
  
  echo "[$(date -u)] DISK ${usage}% >= ${THRESHOLD}% — alerte envoyée"
fi

# Nettoyage logs Docker > 7 jours
find /var/lib/docker/containers -name "*.log" -mtime +7 \
  -exec truncate -s 0 {} \; 2>/dev/null || true
