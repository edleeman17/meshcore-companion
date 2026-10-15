#!/usr/bin/env bash
# Copy the static PWA to wherever your web server serves it from:
#   PWA_DEST=/srv/meshcore-pwa ./pwa/deploy.sh        (local path)
#   PWA_DEST=myhost:/srv/meshcore-pwa ./pwa/deploy.sh (rsync over ssh)
# or set PWA_DEST in ../deploy.env.
set -euo pipefail
cd "$(dirname "$0")"
[ -f ../deploy.env ] && . ../deploy.env
: "${PWA_DEST:?set PWA_DEST}"
# --beta publishes a preview to $PWA_DEST/beta/ (open <site>/beta/index.html).
# Production deploys protect that folder so --delete never wipes a preview.
if [ "${1:-}" = "--beta" ]; then
  DEST="$PWA_DEST/beta"; FILTER=()
else
  DEST="$PWA_DEST"; FILTER=(--filter 'P beta/')
fi
case "$DEST" in *:*) ;; *) mkdir -p "$DEST" ;; esac
rsync -a --delete "${FILTER[@]}" static/ "$DEST"/
echo "published to $DEST"
