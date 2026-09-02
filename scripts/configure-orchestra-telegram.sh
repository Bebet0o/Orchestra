#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C

ROOT="/opt/orchestra"
SECRET_DIR="${ROOT}/secrets"
SECRET_FILE="${SECRET_DIR}/notifications.env"
NOTIFIER="${ROOT}/repo/scripts/orchestra-notifier.py"
COMPOSE="${ROOT}/repo/scripts/orchestra-compose.sh"

read -rsp "Token du bot Telegram : " BOT_TOKEN
echo
read -rp "Chat ID Telegram : " CHAT_ID

[[ "$BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]] || {
    echo "Format de token Telegram invalide." >&2
    exit 1
}
[[ "$CHAT_ID" =~ ^-?[0-9]+$ ]] || {
    echo "Chat ID Telegram invalide." >&2
    exit 1
}

install -d -m 0700 "$SECRET_DIR"
umask 077
TEMP="$(mktemp "${SECRET_DIR}/notifications.env.XXXXXX")"
trap 'rm -f "$TEMP"' EXIT

cat >"$TEMP" <<EOF
ORCHESTRA_TELEGRAM_BOT_TOKEN=${BOT_TOKEN}
ORCHESTRA_TELEGRAM_CHAT_ID=${CHAT_ID}
EOF

chmod 0600 "$TEMP"
mv "$TEMP" "$SECRET_FILE"
trap - EXIT

ORCHESTRA_ROOT="$ROOT" ORCHESTRA_UID="$(id -u)" ORCHESTRA_GID="$(id -g)" \
    "$COMPOSE" up -d --no-deps notifier
for _ in $(seq 1 30); do
    [[ "$(docker inspect --format '{{.State.Status}}' orchestra-notifier 2>/dev/null || true)" == "running" ]] && break
    sleep 1
done

"$NOTIFIER" test-message \
    --channel TELEGRAM \
    --text "Orchestra : notifications Telegram activées." \
    --dedupe-key "telegram-configuration-test-$(date +%s)" \
    --deliver

echo
echo "Configuration Telegram enregistrée dans : ${SECRET_FILE}"
echo "Service : $(docker inspect --format '{{.State.Status}}' orchestra-notifier)"
