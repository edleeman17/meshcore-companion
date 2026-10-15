#!/usr/bin/env bash
# MeshCore Companion: guided installer. Run it ON the machine that will sit
# next to the radio (e.g. a Raspberry Pi), from a copy of this repo:
#
#   git clone https://github.com/edleeman17/meshcore-companion.git
#   cd meshcore-companion && ./install.sh
#
# It checks packages, finds your radio (USB or Bluetooth) and pairs it if needed,
# installs the bridge (which also serves the web app), and optionally sets up
# HTTPS with Tailscale.
# Safe to re-run: it keeps your existing settings and keys.
set -euo pipefail
cd "$(dirname "$0")"
REPO=$PWD
DEST=$HOME/meshcore-bridge
ENVF=$HOME/.config/meshcore-bridge.env
PORT=8190

b=$(tput bold 2>/dev/null || true); g=$(tput setaf 2 2>/dev/null || true); y=$(tput setaf 3 2>/dev/null || true); r=$(tput sgr0 2>/dev/null || true)
step() { echo; echo "${b}━━ $1 ━━${r}"; }
ok() { echo "${g}✓${r} $1"; }
warn() { echo "${y}!${r} $1"; }
ask() { local a; read -r -p "$1 " a </dev/tty; echo "$a"; }
confirm() { local a; a=$(ask "$1 [Y/n]"); [[ -z "$a" || "$a" =~ ^[Yy] ]]; }

echo "${b}MeshCore Companion installer${r}"
echo "This sets up the bridge on $(hostname), for a radio on USB or Bluetooth. Takes about 5–10 minutes."

# ---------------------------------------------------------------- 1
step "1/6  Checking packages"
need=()
command -v python3 >/dev/null || need+=(python3)
python3 -c "import venv, ensurepip" 2>/dev/null || need+=(python3-venv)
command -v bluetoothctl >/dev/null || need+=(bluez)
command -v curl >/dev/null || need+=(curl)
if ((${#need[@]})); then
  warn "Missing: ${need[*]}"
  if confirm "Install them now with sudo apt?"; then sudo apt-get update -qq && sudo apt-get install -y "${need[@]}"; else exit 1; fi
fi
pyv=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || { warn "Python $pyv found; 3.11+ is needed (Raspberry Pi OS Bookworm or newer)."; exit 1; }
ok "Python $pyv, BlueZ"
if ! id -nG | grep -qw bluetooth && getent group bluetooth >/dev/null; then
  sudo usermod -aG bluetooth "$USER" && warn "Added you to the 'bluetooth' group. If pairing fails below, log out and back in, then re-run."
fi
loginctl show-user "$USER" 2>/dev/null | grep -q Linger=yes || { sudo loginctl enable-linger "$USER" && ok "Bridge will start at boot"; }

# ---------------------------------------------------------------- 2
step "2/6  Finding your radio"
SERIAL=$(sed -n 's/^RADIO_SERIAL=//p' "$ENVF" 2>/dev/null | head -1 || true)
ADDR=$(sed -n 's/^RADIO_ADDRESS=//p' "$ENVF" 2>/dev/null | head -1 || true)
if [[ -n "$SERIAL$ADDR" ]] && confirm "Use the radio already set up (${SERIAL:-$ADDR})?"; then
  :
else
  SERIAL=""; ADDR=""
  echo "  1) USB cable    — radio plugged into this machine (USB companion firmware). Most reliable."
  echo "  2) Bluetooth    — radio nearby, no cable (BLE companion firmware)"
  how=$(ask "How is the radio connected? [1-2]:")
  if [[ "$how" == 1 ]]; then
    mapfile -t ports < <(ls /dev/serial/by-id/* 2>/dev/null; ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null)
    if ((${#ports[@]} == 0)); then
      warn "No USB serial device found. Plug the radio in (a data cable, not charge-only) and re-run."; exit 1
    elif ((${#ports[@]} == 1)); then
      SERIAL=${ports[0]}; ok "Found $SERIAL"
    else
      for i in "${!ports[@]}"; do echo "  $((i + 1))) ${ports[$i]}"; done
      n=$(ask "Which one? [1-${#ports[@]}]"); SERIAL=${ports[$((n - 1))]}
    fi
  else
    echo "Make sure the radio is on and NOT connected to the MeshCore phone app."
    echo "Scanning for 20 seconds…"
    mapfile -t found < <(bluetoothctl --timeout 20 scan on 2>/dev/null | grep -oE '([0-9A-F]{2}:){5}[0-9A-F]{2} MeshCore-[^ ]*' | sort -u)
    if ((${#found[@]} == 0)); then
      warn "No MeshCore radio found. Check it runs the Companion (BLE) firmware and is in range."
      ADDR=$(ask "Or type its Bluetooth address (AA:BB:CC:DD:EE:FF):")
    elif ((${#found[@]} == 1)); then
      ADDR=${found[0]%% *}; ok "Found ${found[0]}"
    else
      for i in "${!found[@]}"; do echo "  $((i + 1))) ${found[$i]}"; done
      n=$(ask "Which one? [1-${#found[@]}]"); ADDR=${found[$((n - 1))]%% *}
    fi
    [[ "$ADDR" =~ ^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$ ]] || { warn "That isn't a Bluetooth address."; exit 1; }
  fi
fi

# ---------------------------------------------------------------- 3
if [[ -n "$SERIAL" ]]; then
  step "3/6  USB access"
  if id -nG | grep -qw dialout; then
    ok "You can use the USB port"
  else
    sudo usermod -aG dialout "$USER"
    warn "Added you to the 'dialout' group so the bridge can open the USB port."
    warn "Reboot once (sudo reboot), then run ./install.sh again to finish."
    exit 0
  fi
else
  step "3/6  Pairing"
  if bluetoothctl info "$ADDR" 2>/dev/null | grep -q "Paired: yes" && confirm "Already paired. Keep the existing pairing?"; then
    ok "Keeping pairing"
  else
    PIN=$(ask "PIN shown on the radio's screen (boards without a screen: 123456):")
    "$REPO/bridge/pair.sh" "$ADDR" "${PIN:-123456}"
  fi
fi

# ---------------------------------------------------------------- 4
step "4/6  Installing the bridge"
mkdir -p "$DEST" "$HOME/.config/systemd/user" "$HOME/.local/share/meshcore-bridge"
rm -rf "$DEST/app" && cp -r "$REPO/bridge/app" "$DEST/app"
cp "$REPO/bridge/requirements.txt" "$REPO/bridge/backup.sh" "$REPO/bridge/pair.sh" "$DEST/"
rm -rf "$DEST/pwa" && cp -r "$REPO/pwa/static" "$DEST/pwa"
[[ -d "$DEST/venv" ]] || python3 -m venv "$DEST/venv"
echo "Installing Python packages (a few minutes on a Pi Zero)…"
"$DEST/venv/bin/pip" -q install --upgrade pip >/dev/null
"$DEST/venv/bin/pip" -q install -r "$DEST/requirements.txt"
ok "Bridge installed in $DEST"
if [[ -n "$SERIAL" ]]; then CONN="RADIO_SERIAL=$SERIAL"; else CONN="RADIO_ADDRESS=$ADDR"; fi
if [[ ! -f "$ENVF" ]]; then
  umask 077
  "$DEST/venv/bin/python" - "$CONN" > "$ENVF" <<'PY'
import base64, sys
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
k = ec.generate_private_key(ec.SECP256R1())
b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
priv = b64(k.private_numbers().private_value.to_bytes(32, "big"))
pub = b64(k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
print(f"{sys.argv[1]}\nVAPID_PRIVATE_KEY={priv}\nVAPID_PUBLIC_KEY={pub}\nVAPID_SUBJECT=mailto:admin@example.com")
PY
  ok "Created $ENVF with fresh push-notification keys"
else
  # one connection line: drop the old USB/Bluetooth setting, write the chosen one
  sed -i '/^RADIO_SERIAL=/d; /^RADIO_ADDRESS=/d' "$ENVF" && echo "$CONN" >> "$ENVF"
  ok "Kept your settings in $ENVF"
fi
cp "$REPO/bridge/meshcore-bridge.service" "$REPO/bridge/meshcore-backup.service" "$REPO/bridge/meshcore-backup.timer" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable -q meshcore-bridge && systemctl --user enable -q --now meshcore-backup.timer
systemctl --user restart meshcore-bridge

# ---------------------------------------------------------------- 5
step "5/6  Connecting to the radio"
for _ in $(seq 1 45); do
  curl -s "localhost:$PORT/health" | grep -q '"connected":true' && { ok "Radio connected"; break; }
  sleep 2
done
curl -s "localhost:$PORT/health" | grep -q '"connected":true' || warn "Not connected yet. It keeps retrying; see Troubleshooting in the README. Log: journalctl --user -u meshcore-bridge"

# ---------------------------------------------------------------- 6
step "6/6  How will you open the app?"
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
URL="http://$IP:$PORT/"
echo "  1) Just on this network, plain HTTP — works now, but no notifications or home-screen install"
echo "  2) Tailscale (recommended) — HTTPS with a real certificate, notifications, and it works away from home"
echo "  3) My own web server / reverse proxy — I'll set up HTTPS myself"
choice=$(ask "Choose [1-3]:")
case "$choice" in
  2)
    if ! command -v tailscale >/dev/null; then
      confirm "Install Tailscale now (runs its official install script with sudo)?" && curl -fsSL https://tailscale.com/install.sh | sh
    fi
    if command -v tailscale >/dev/null; then
      sudo tailscale status >/dev/null 2>&1 || sudo tailscale up
      echo "In the Tailscale admin console (DNS page) turn on MagicDNS and HTTPS Certificates if asked."
      sudo tailscale serve --bg "$PORT"
      TS=$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)
      [[ -n "$TS" ]] && URL="https://$TS/"
      echo "Install the Tailscale app on your phone and sign in with the same account."
    fi ;;
  3) echo "Proxy everything to http://$IP:$PORT (the bridge serves the app and /api)."
     echo "Example nginx config: $REPO/pwa/nginx.conf.template — keep 'proxy_buffering off' for /api/stream." ;;
  *) ;;
esac

echo
echo "${b}All done.${r} Open this on your phone or computer:"
echo
echo "    ${b}$URL${r}"
command -v qrencode >/dev/null && qrencode -t ANSIUTF8 "$URL"
echo
echo "Then: Share → Add to Home Screen, open it from the icon, Radio → Enable notifications."
echo "Re-run ./install.sh any time to update; your settings are kept."
