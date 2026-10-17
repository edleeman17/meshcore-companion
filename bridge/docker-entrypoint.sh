#!/bin/sh
# Web Push needs a VAPID key pair that stays the same across restarts, or phones
# stop getting notifications. If none is set, make one once and keep it in DATA_DIR.
set -e
if [ -z "$VAPID_PRIVATE_KEY" ]; then
  keys="$DATA_DIR/vapid.env"
  if [ ! -f "$keys" ]; then
    (umask 077; python - > "$keys" <<'PY'
import base64
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
k = ec.generate_private_key(ec.SECP256R1())
b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
print(f"VAPID_PRIVATE_KEY={b64(k.private_numbers().private_value.to_bytes(32, 'big'))}")
print(f"VAPID_PUBLIC_KEY={b64(k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))}")
PY
    )
    echo "created $keys with fresh push-notification keys"
  fi
  set -a; . "$keys"; set +a
fi
exec "$@"
