#!/usr/bin/env bash
# ================================================================
# deploy.sh — Nemesis Trading Bot — Deploy via SSH key
# Auth: SSH key (~/.ssh/id_ed25519) — aucun mot de passe en clair
# Usage: ./deploy.sh <commande> [args]
# ================================================================
set -e

VPS_HOST="root@46.225.186.152"
BOT_CONTAINER="nemesis_bot"
MONITOR_CONTAINER="monitor"

SSH="ssh -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=no $VPS_HOST"
SCP="scp -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=no"

# ── Fonctions ────────────────────────────────────────────────────

deploy_file() {
    local LOCAL_FILE="$1"
    local CONTAINER_PATH="$2"
    local CONTAINER="${3:-$BOT_CONTAINER}"
    local TMP_PATH="/tmp/_deploy_$(basename $LOCAL_FILE)"

    echo "📦 Deploy: $LOCAL_FILE → $CONTAINER:$CONTAINER_PATH"
    $SCP "$LOCAL_FILE" "$VPS_HOST:$TMP_PATH"
    $SSH "docker exec -i $CONTAINER sh -c 'cat > $CONTAINER_PATH' < $TMP_PATH && rm -f $TMP_PATH"
    echo "✅ $LOCAL_FILE injecté dans $CONTAINER"
}

restart_bot() {
    echo "🔄 Restart nemesis_bot..."
    $SSH "docker restart $BOT_CONTAINER && sleep 5 && docker logs $BOT_CONTAINER --tail=5 2>&1"
}

restart_monitor() {
    echo "🔄 Restart monitor..."
    $SSH "docker restart $MONITOR_CONTAINER && echo OK"
}

status() {
    echo "📊 Statut containers..."
    $SSH 'docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Image}}"'
}

logs() {
    $SSH "docker logs $BOT_CONTAINER --tail=${2:-20} 2>&1"
}

backup_now() {
    echo "💾 Backup PostgreSQL maintenant..."
    $SSH "/root/backup_postgres.sh"
}

# ── Commandes ────────────────────────────────────────────────────

case "$1" in
    "deploy")
        [ -z "$2" ] || [ -z "$3" ] && { echo "Usage: ./deploy.sh deploy <fichier> <path_container>"; exit 1; }
        deploy_file "$2" "$3" "${4:-$BOT_CONTAINER}"
        ;;
    "restart")     restart_bot ;;
    "restart-monitor") restart_monitor ;;
    "status")      status ;;
    "logs")        logs "${@:2}" ;;
    "backup")      backup_now ;;
    *)
        echo "Nemesis Deploy Tool (SSH key-based auth)"
        echo ""
        echo "Commandes:"
        echo "  ./deploy.sh deploy <fichier_local> <chemin_container> [container]"
        echo "  ./deploy.sh restart"
        echo "  ./deploy.sh restart-monitor"
        echo "  ./deploy.sh status"
        echo "  ./deploy.sh logs [lines]"
        echo "  ./deploy.sh backup"
        ;;
esac
