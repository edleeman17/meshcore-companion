#!/usr/bin/env bash
# Pair this machine with a MeshCore companion radio over BLE (run ON the bridge host).
#   ./pair.sh AA:BB:CC:DD:EE:FF 123456
# The PIN is shown on the radio's screen (or set a fixed one, see README).
# Waits for the passkey prompt instead of guessing timings, which is what makes
# scripted bluetoothctl pairing flaky.
set -euo pipefail
ADDR=${1:?radio BLE address}; PIN=${2:?6-digit PIN}
systemctl --user stop meshcore-bridge 2>/dev/null || true
bluetoothctl disconnect "$ADDR" >/dev/null 2>&1 || true
bluetoothctl remove "$ADDR" >/dev/null 2>&1 || true
OUT=$(mktemp); FIFO=$(mktemp -u); mkfifo "$FIFO"
trap 'rm -f "$OUT" "$FIFO"' EXIT
( bluetoothctl < "$FIFO" > "$OUT" 2>&1 ) &
exec 3>"$FIFO"
echo "agent KeyboardOnly" >&3; echo "default-agent" >&3; echo "scan on" >&3
for _ in $(seq 1 30); do grep -aq "$ADDR" "$OUT" && break; sleep 1; done
echo "scan off" >&3; sleep 1
echo "pair $ADDR" >&3
for _ in $(seq 1 60); do grep -aq "Enter passkey" "$OUT" && break; sleep 0.3; done
echo "$PIN" >&3
for _ in $(seq 1 60); do grep -aqE "Pairing successful|Failed to pair" "$OUT" && break; sleep 0.5; done
echo "trust $ADDR" >&3; sleep 1
echo "disconnect $ADDR" >&3; sleep 2; echo "quit" >&3; exec 3>&-
sleep 1
if grep -aq "Pairing successful" "$OUT"; then
  echo "paired with $ADDR"
  systemctl --user start meshcore-bridge 2>/dev/null || true
else
  echo "pairing failed: $(grep -aoE 'Failed to pair: [A-Za-z.]*' "$OUT" | tail -1)"; exit 1
fi
