#!/usr/bin/env bash
# Deploy the bridge to a Linux host with Bluetooth (e.g. a Raspberry Pi) as a
# systemd *user* service in ~/meshcore-bridge. Run from your workstation:
#
#   HOST=mypi RADIO_ADDRESS=AA:BB:CC:DD:EE:FF ./bridge/deploy.sh   (Bluetooth)
#   HOST=mypi RADIO_SERIAL=/dev/ttyACM0 ./bridge/deploy.sh         (USB)
#   HOST=mypi RADIO_HOST=10.0.0.50 ./bridge/deploy.sh           (WiFi, companion firmware v1.18+)
#
# or put those in ../deploy.env (git-ignored). The first run also creates
# ~/.config/meshcore-bridge.env on the host with fresh VAPID (Web Push) keys.
set -euo pipefail
cd "$(dirname "$0")"
[ -f ../deploy.env ] && . ../deploy.env
: "${HOST:?set HOST (ssh host of the bridge machine)}"
: "${RADIO_ADDRESS:=}" "${RADIO_SERIAL:=}" "${RADIO_HOST:=}"
[ -n "$RADIO_ADDRESS$RADIO_SERIAL$RADIO_HOST" ] || { echo "set RADIO_HOST (WiFi ip), RADIO_SERIAL (USB port) or RADIO_ADDRESS (Bluetooth MAC)"; exit 1; }
if [ -n "$RADIO_HOST" ]; then CONN="RADIO_HOST=$RADIO_HOST"
elif [ -n "$RADIO_SERIAL" ]; then CONN="RADIO_SERIAL=$RADIO_SERIAL"; else CONN="RADIO_ADDRESS=$RADIO_ADDRESS"; fi
VAPID_SUBJECT=${VAPID_SUBJECT:-mailto:admin@example.com}

ssh "$HOST" 'mkdir -p ~/meshcore-bridge ~/.local/share/meshcore-bridge ~/.config/systemd/user'
rsync -a --delete --exclude __pycache__ --exclude venv app requirements.txt backup.sh pair.sh mux.py "$HOST":meshcore-bridge/
rsync -a --delete ../pwa/static/ "$HOST":meshcore-bridge/pwa/   # the bridge serves the web app too
ssh "$HOST" 'cd ~/meshcore-bridge && { [ -d venv ] || python3 -m venv venv; } && venv/bin/pip -q install -r requirements.txt'

ssh "$HOST" 'test -f ~/.config/meshcore-bridge.env' || {
  echo "creating ~/.config/meshcore-bridge.env with new VAPID keys"
  ssh "$HOST" "cd ~/meshcore-bridge && umask 077 && venv/bin/python - '$CONN' '$VAPID_SUBJECT' > ~/.config/meshcore-bridge.env" <<'PY'
import base64, sys
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
k = ec.generate_private_key(ec.SECP256R1())
b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
priv = b64(k.private_numbers().private_value.to_bytes(32, "big"))
pub = b64(k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
print(f"{sys.argv[1]}\nVAPID_PRIVATE_KEY={priv}\nVAPID_PUBLIC_KEY={pub}\nVAPID_SUBJECT={sys.argv[2]}")
PY
}
if [ -n "$RADIO_HOST" ]; then     # switching an existing install to WiFi
  ssh "$HOST" "grep -q '^RADIO_HOST=' ~/.config/meshcore-bridge.env && sed -i 's|^RADIO_HOST=.*|RADIO_HOST=$RADIO_HOST|' ~/.config/meshcore-bridge.env || echo 'RADIO_HOST=$RADIO_HOST' >> ~/.config/meshcore-bridge.env"
fi
if [ -n "$RADIO_SERIAL" ]; then   # switching an existing install to USB
  ssh "$HOST" "grep -q '^RADIO_SERIAL=' ~/.config/meshcore-bridge.env && sed -i 's|^RADIO_SERIAL=.*|RADIO_SERIAL=$RADIO_SERIAL|' ~/.config/meshcore-bridge.env || echo 'RADIO_SERIAL=$RADIO_SERIAL' >> ~/.config/meshcore-bridge.env"
fi
if [ -n "${DISCOVER_NAMES:-}" ]; then
  ssh "$HOST" "grep -q '^DISCOVER_NAMES=' ~/.config/meshcore-bridge.env && sed -i 's|^DISCOVER_NAMES=.*|DISCOVER_NAMES=$DISCOVER_NAMES|' ~/.config/meshcore-bridge.env || echo 'DISCOVER_NAMES=$DISCOVER_NAMES' >> ~/.config/meshcore-bridge.env"
fi
if [ -n "${BACKUP_DIR:-}" ]; then
  ssh "$HOST" "grep -q '^BACKUP_DIR=' ~/.config/meshcore-bridge.env || echo 'BACKUP_DIR=$BACKUP_DIR' >> ~/.config/meshcore-bridge.env"
fi

scp -q meshcore-bridge.service meshcore-mux.service meshcore-backup.service meshcore-backup.timer "$HOST":.config/systemd/user/
ssh "$HOST" 'systemctl --user daemon-reload && systemctl --user enable -q meshcore-bridge && systemctl --user enable -q --now meshcore-backup.timer && systemctl --user restart meshcore-bridge'
# a radio shared through mux.py (MUX_RADIO set in the env file): keep the mux running too
ssh "$HOST" 'grep -q "^MUX_RADIO=" ~/.config/meshcore-bridge.env && systemctl --user enable -q meshcore-mux && systemctl --user restart meshcore-mux || true'
sleep 10
ssh "$HOST" 'systemctl --user is-active meshcore-bridge; curl -s localhost:8190/health; echo'
