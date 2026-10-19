#!/usr/bin/env bash
# Real kitty test harness: runs kitty (software GL via nix mesa/llvmpipe) on an X display and drives it through
# kitty remote control (text) and XTEST (real key and mouse events).
# Usage: harness.sh start [-- cmd args...] | stop | status | ls | sock | wid | raise
#        | send <text> | sendkey <key>... | text [get-text args] | wait-text <regex> [timeout_s] | kitten <args...>
#        | screenshot <out.png> [--window] | xkey <keysym>... | xtype <text> | xclick <x> <y> [btn] | xmove <x> <y>
# Env: KH_DIR (/tmp/claude-1000/tgi-e2e/kh) state dir; the kitty class and socket are derived from it, so
#      instances with different KH_DIRs never collide.  KH_DISPLAY (:175) KH_W KH_H KH_FONT KH_KITTY_OPTS
#      KH_NIXPKGS (flake ref, default nixpkgs)
set -euo pipefail
HDIR="${KH_DIR:-/tmp/claude-1000/tgi-e2e/kh}"
export DISPLAY="${KH_DISPLAY:-:175}"
# unix socket paths are limited to 108 bytes -> use a Linux abstract socket keyed on HDIR
ID="kh-$(printf %s "$HDIR" | md5sum | cut -c1-12)"
SOCKNAME=$ID CLASS=$ID PIDF="$HDIR/kitty.pid" LOG="$HDIR/kitty.log" PATHS="$HDIR/.nixpaths"
NIXPKGS="${KH_NIXPKGS:-nixpkgs}"

resolve() {  # cache store paths of kitty/mesa/imagemagick/xdotool
  mkdir -p "$HDIR"
  if [[ ! -s $PATHS ]]; then
    nix build --no-link --print-out-paths "$NIXPKGS#kitty" "$NIXPKGS#mesa" "$NIXPKGS#imagemagick" "$NIXPKGS#xdotool" \
      | grep -v -- '-man$\|-doc$\|-dev$' > "$PATHS.tmp"
    mv "$PATHS.tmp" "$PATHS"
  fi
  KITTY=$(grep -- '-kitty-[0-9]' "$PATHS" | head -1)
  MESA=$(grep -- '-mesa-[0-9]' "$PATHS" | head -1)
  IM=$(grep -- '-imagemagick-[0-9]' "$PATHS" | head -1)
  XDO=$(grep -- '-xdotool-[0-9]' "$PATHS" | head -1)
}
running() { [[ -s $PIDF ]] && kill -0 "$(cat "$PIDF")" 2>/dev/null; }
kat() { resolve; "$KITTY/bin/kitten" @ --to "unix:@$SOCKNAME" "$@"; }
wid() { resolve; "$XDO/bin/xdotool" search --classname "$CLASS" | head -1; }
# XTEST input goes to the focused window, so focus and raise ours first (XSendEvent via --window loses modifiers)
activate() { local w; w=$(wid); "$XDO/bin/xdotool" windowactivate --sync "$w" windowraise "$w"; echo "$w"; }

cmd="${1:-}"; shift || true
case "$cmd" in
  start)
    running && { echo "already running pid $(cat "$PIDF")"; exit 0; }
    resolve
    [[ "${1:-}" == "--" ]] && shift
    # don't leak the GL workaround env into the shell / programs under test (e.g. xpra):
    childenv=(-o "env=__GLX_VENDOR_LIBRARY_NAME" -o "env=LIBGL_ALWAYS_SOFTWARE" -o "env=GALLIUM_DRIVER"
              -o "env=__EGL_VENDOR_LIBRARY_FILENAMES" -o "env=KITTY_CONFIG_DIRECTORY"
              -o "env=PATH=$KITTY/bin:${PATH}")
    if [[ -n ${LD_LIBRARY_PATH:-} ]]; then childenv+=(-o "env=LD_LIBRARY_PATH=$LD_LIBRARY_PATH"); else childenv+=(-o "env=LD_LIBRARY_PATH"); fi
    # nix-built kitty on non-NixOS: libglvnd can't find a GLX vendor lib -> point it at nix mesa (llvmpipe).
    # shellcheck disable=SC2086  # KH_KITTY_OPTS is a word list
    env LD_LIBRARY_PATH="$MESA/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
        __GLX_VENDOR_LIBRARY_NAME=mesa LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=llvmpipe \
        __EGL_VENDOR_LIBRARY_FILENAMES="$MESA/share/glvnd/egl_vendor.d/50_mesa.json" \
        KITTY_CONFIG_DIRECTORY="$HDIR/conf" \
      setsid "$KITTY/bin/kitty" --class "$CLASS" --title "$CLASS" \
        -o allow_remote_control=yes --listen-on "unix:@$SOCKNAME" \
        -o linux_display_server=x11 -o confirm_os_window_close=0 \
        -o initial_window_width="${KH_W:-1000}" -o initial_window_height="${KH_H:-700}" -o remember_window_size=no \
        -o font_size="${KH_FONT:-11}" -o enable_audio_bell=no -o update_check_interval=0 \
        "${childenv[@]}" ${KH_KITTY_OPTS:-} "$@" >"$LOG" 2>&1 < /dev/null &
    echo $! > "$PIDF"
    for _ in $(seq 100); do kat ls >/dev/null 2>&1 && { echo "started pid $(cat "$PIDF") class $CLASS sock unix:@$SOCKNAME"; exit 0; }
      running || { echo "kitty died:"; cat "$LOG"; exit 1; }; sleep 0.1; done
    echo "timeout waiting for socket"; cat "$LOG"; exit 1 ;;
  send) kat send-text -- "$*" ;;            # supports \n \r \x1b escapes (python-style)
  sendkey) kat send-key "$@" ;;             # e.g. sendkey ctrl+c enter
  text) kat get-text "$@" ;;                # e.g. text --extent all
  ls) kat ls ;;
  sock) echo "unix:@$SOCKNAME" ;;
  kitten) kat "$@" ;;
  wid) wid ;;
  raise) activate >/dev/null ;;
  wait-text)
    re="$1"; t="${2:-10}"; end=$((SECONDS+t))
    while (( SECONDS < end )); do kat get-text 2>/dev/null | grep -Eq -- "$re" && exit 0; sleep 0.2; done
    echo "wait-text timeout: $re"; exit 1 ;;
  screenshot)
    resolve; out="${1:?out.png}"; mode="${2:-}"
    w=$(activate)  # other windows on the display can overlap ours
    if [[ $mode == --window ]]; then "$IM/bin/import" -display "$DISPLAY" -window "$w" "$out"
    else "$IM/bin/import" -display "$DISPLAY" -window root "$out"; fi
    echo "$out" ;;
  xkey)   # real X11 key events through kitty's keyboard encoder, e.g. xkey ctrl+shift+a Return
    resolve; activate >/dev/null; "$XDO/bin/xdotool" key "$@" ;;
  xtype)  resolve; activate >/dev/null; "$XDO/bin/xdotool" type -- "$*" ;;
  xclick) # xclick <x> <y> [button]  (window-relative pixels; kitty centers the cell grid)
    resolve; w=$(activate); "$XDO/bin/xdotool" mousemove --window "$w" "$1" "$2" click "${3:-1}" ;;
  xmove)  resolve; w=$(activate); "$XDO/bin/xdotool" mousemove --window "$w" "$1" "$2" ;;
  status) if running; then echo "running pid $(cat "$PIDF")"; else echo "stopped"; fi ;;
  stop)
    if running; then pid=$(cat "$PIDF"); kill "$pid"; for _ in $(seq 30); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
      kill -0 "$pid" 2>/dev/null && kill -9 "$pid"; echo "stopped $pid"; else echo "not running"; fi
    rm -f "$PIDF" ;;
  *) sed -n '2,9p' "$0"; exit 2 ;;
esac
