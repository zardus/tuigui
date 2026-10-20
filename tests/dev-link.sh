#!/usr/bin/env bash
# Builds a runnable dev tg without building the flake: substitutes src/tg.py's @placeholders@ with existing
# store paths. OUT (default /tmp/tg-dev-$UID) receives tg and share/; XPRA overrides the xpra store
# path (default: this flake's .#xpra, which builds it once).
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
OUT=${OUT:-/tmp/tg-dev-$UID}
XPRA=${XPRA:-$(nix build --no-link --print-out-paths "$repo#xpra")}
XPRA=$(readlink -f "$XPRA")
# nixpkgs as locked by this flake, so the X server matches the one in xpra's xorg.conf
pkg() { nix build --no-link --print-out-paths --inputs-from "$repo" "nixpkgs#$1"; }
XORGSERVER=$(pkg xorg-server)
MATCHBOX=$(nix build --no-link --print-out-paths "$repo#matchbox")   # patched, see nix/matchbox.nix
PY=$(pkg python3)
xpra_version=${XPRA#*-xpra-}      # from the store path name; running xpra here could write ~/.config/xpra

mkdir -p "$OUT/share"
cp "$repo/src/share/matchbox-kbdconfig" "$OUT/share/"
sed -e "s|@python3@|$PY/bin/python3|" -e "s|@xpra@|$XPRA|" -e "s|@xorgserver@|$XORGSERVER|" \
    -e "s|@matchbox@|$MATCHBOX|" -e "s|@share@|$OUT/share|" -e "s|@version@|dev (xpra $xpra_version)|" \
    "$repo/src/tg.py" > "$OUT/tg.new"
chmod 755 "$OUT/tg.new"
mv "$OUT/tg.new" "$OUT/tg"
echo "$OUT/tg"
