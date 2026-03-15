#!/bin/bash
# start.sh — Lance gost SOCKS5 proxy puis le bot

# Si gost est disponible et que le proxy WARP est configuré
if command -v gost &>/dev/null && [ -n "$HTTPS_PROXY" ]; then
    echo "[start.sh] Démarrage gost proxy SOCKS5 (chainage vers WARP)..."
    # gost écoute sur localhost:1080, forward vers WARP via socat (172.17.0.1:40001)
    # Utilise le port socat comme upstream
    WARP_HOST=$(echo "$HTTPS_PROXY" | sed 's|socks5h\?://||')
    gost -L socks5://127.0.0.1:1080 -F socks5://${WARP_HOST} \
        > /tmp/gost.log 2>&1 &
    GOST_PID=$!
    sleep 2
    if kill -0 $GOST_PID 2>/dev/null; then
        echo "[start.sh] gost actif PID=$GOST_PID — proxy local socks5://127.0.0.1:1080"
        # Override proxy vers gost local (pas socat)
        export HTTPS_PROXY="socks5h://127.0.0.1:1080"
        export HTTP_PROXY="socks5h://127.0.0.1:1080"
        export ALL_PROXY="socks5h://127.0.0.1:1080"
    else
        echo "[start.sh] gost échoué — on continue sans proxy local"
    fi
else
    echo "[start.sh] gost non disponible ou HTTPS_PROXY non défini — démarrage direct"
fi

exec python3 main.py
