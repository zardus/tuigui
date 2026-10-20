#!/usr/bin/env bash
# End-to-end test of tg in a real kitty, driven by tests/kitty/harness.sh on an X display.
#
# usage: TG=/path/to/tg KH_DISPLAY=:N [KH_DIR=DIR] [TG_E2E_DIR=DIR] tests/kitty/e2e.sh [STEP...]
#   TG              tg to test (required), e.g. the dev-linked one from tests/dev-link.sh
#   KH_DISPLAY      a dedicated X display for kitty, e.g. an Xvfb on :175 (required; gets XTEST input)
#   KH_DIR          harness state dir (default /tmp/tg-e2e-$UID/kh); step 10 adds a second one, KH_DIR-2
#   TG_E2E_DIR      parent of the per-round runtime dirs, wiped at start; keep it short (default /tmp/tg-e2e-$UID/rt).
#                   Not TG_RUNTIME_DIR: the suite deletes it and kills every process that mentions it.
#   STEP            any of 1 3 4 5 6 2 7 8 9 10 11 (default: all, in that order: 3-6 reuse the session of step 1)
#
# Steps: 1 fill + size   2 typing   3 resize   4 pointer   5 window switching   6 shortcuts + detach
#        7 app exit      8 client crash   9 hangup   10 two concurrent sessions   11 GTK popup menus
# Screenshots go to tests/kitty/out/. Keyboard input is XTEST into the focused kitty window.
set -uo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
: "${TG:?set TG to the tg under test}"
H=$HERE/harness.sh
export KH_DIR=${KH_DIR:-/tmp/tg-e2e-$UID/kh}
KH_DIR2=$KH_DIR-2
RT=${TG_E2E_DIR:-/tmp/tg-e2e-$UID/rt}
[[ -z ${TG_RUNTIME_DIR:-} || $RT != "$TG_RUNTIME_DIR" ]] ||
  { echo "refusing to wipe TG_RUNTIME_DIR=$TG_RUNTIME_DIR; set TG_E2E_DIR instead"; exit 2; }
unset TG_RUNTIME_DIR
OUT=$HERE/out
: "${KH_DISPLAY:?set KH_DISPLAY to a dedicated X display for the kitty harness (e.g. an Xvfb on :175)}"
export KH_DISPLAY
PASSES=0 FAILS=0
R=""  # session dir of the round in progress

pass() { PASSES=$((PASSES + 1)); echo "PASS $*"; }
fail() { FAILS=$((FAILS + 1)); echo "FAIL $*"; }
check() { local name=$1; shift; if "$@"; then pass "$name"; else fail "$name"; return 1; fi; }
info() { echo "     $*"; }

# nixbin PKG BIN: a tool from nixpkgs as locked by this repo's flake
nixbin() { echo "$(nix build --no-link --print-out-paths --inputs-from "$REPO" "nixpkgs#$1" | head -1)/bin/$2"; }
PY3=$(nixbin python3 python3)   # the harness kitty shell may have no python3 on PATH

onhost() { DISPLAY=$KH_DISPLAY "$@"; }
# on R's X display
inx() { DISPLAY=$(display_of "$R") XAUTHORITY=$R/Xauthority "$@"; }
display_of() { python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["display"])' "$1/meta.json"; }
app_pid() { python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["pids"]["app"])' "$1/meta.json"; }
xsize() { inx "$XRANDR" -q 2>/dev/null | sed -n 's/.*current \([0-9]*\) x \([0-9]*\).*/\1x\2/p'; }
wait_xsize() {
  local end=$((SECONDS + $2))
  while ((SECONDS < end)); do [[ $(xsize) == "$1" ]] && return 0; sleep 0.2; done
  info "X size is $(xsize), wanted $1"; return 1
}

# round NAME TG_ARGS...: type a tg command into the kitty shell of harness $KH_DIR; it ends by printing tg-rc-NAME=<status>
round() {
  local name=$1; shift
  mkdir -m 700 "$RT/$name"
  {
    printf 'TG_RUNTIME_DIR=%q %q' "$RT/$name" "$TG"
    printf ' %q' "$@"
    printf '\necho tg-rc-%s=$?\n' "$name"
  } >"$RT/$name.sh"
  "$H" send "clear; bash $RT/$name.sh\\r" >/dev/null
}

# meta_ready META_JSON: tg has recorded the display and launched the program
meta_ready() {
  python3 -I -c 'import json,sys; m=json.load(open(sys.argv[1])); sys.exit(not (m["display"] and m["pids"]["app"]))' \
    "$1" 2>/dev/null
}

# wait_session NAME: set R once the round's tg has started its program, then wait for the client's graphics phase
wait_session() {
  local end=$((SECONDS + 90)) m
  R=""
  while ((SECONDS < end)) && [[ -z $R ]]; do
    for m in "$RT/$1"/*/meta.json; do meta_ready "$m" && R=$(dirname "$m"); done
    sleep 0.2
  done
  [[ -n $R ]] || { info "no session for round $1"; return 1; }
  until compgen -G "$R/xpra/xpra-terminal-*.log" >/dev/null || ((SECONDS > end)); do sleep 0.2; done
  sleep 2  # first frame
}

# wait_rc NAME TIMEOUT: print tg's exit status for round NAME
wait_rc() {
  "$H" wait-text "tg-rc-$1=[0-9]+" "$2" >/dev/null || return 1
  "$H" text | sed -n "s/.*tg-rc-$1=\([0-9]*\).*/\1/p" | tail -1
}

# tty_cooked TAG: the shell answers at column 0 (cooked mode, no staircase).
# Retried once: right after tg exits, kitty's send-text occasionally lost the first characters (seen once in step 6).
tty_cooked() {
  local try
  for try in 1 2; do
    "$H" send "echo tty-\$((40+$1))\\r" >/dev/null
    "$H" wait-text "tty-$((40 + $1))" 5 >/dev/null && "$H" text | grep -q "^tty-$((40 + $1))" && return 0
    info "tty_cooked: no answer to attempt $try"; sleep 1
  done
  return 1
}

shot() { "$H" screenshot "$OUT/$1.png" --window >/dev/null; echo "$OUT/$1.png"; }

# pixels FILE COLOUR: number of pixels of exactly COLOUR
pixels() {
  "$MAGICK" "$1" -fill black +opaque "$2" -fill white -opaque "$2" -format '%[fx:int(mean*w*h)]' info:
}

# measure_cells: CW x CH, the cell size of the harness kitty, from TIOCGWINSZ in its shell; TW x TH, the text area
# of the 1000x700 window. The expected X screen sizes follow from it (the harness kitty has no window padding).
measure_cells() {
  "$H" send "clear; $PY3 -c 'import fcntl,termios,struct; r,c,x,y=struct.unpack(\"HHHH\",fcntl.ioctl(0,termios.TIOCGWINSZ,bytes(8))); print(\"tg-cell\", x//c, y//r)'\\r" >/dev/null
  "$H" wait-text 'tg-cell [0-9]+ [0-9]+' 10 >/dev/null || return 1
  read -r CW CH < <("$H" text | sed -n 's/^tg-cell \([0-9]*\) \([0-9]*\).*/\1 \2/p' | tail -1)
  ((CW > 0 && CH > 0)) || return 1
  TW=$((1000 / CW * CW)) TH=$((700 / CH * CH))
  info "cell ${CW}x${CH}, text area of a 1000x700 window ${TW}x${TH}"
}
# expect W H: the X screen size for a W x H kitty window
expect() { echo "$(($1 / CW * CW))x$(($2 / CH * CH))"; }

# wait_red NAME: a red xterm covers >= 60% of the text area within 90 s
wait_red() {
  local end=$((SECONDS + 90)) n=0 f
  while ((SECONDS < end)); do
    f=$(shot "$1"); n=$(pixels "$f" red)
    ((n >= TW * TH * 6 / 10)) && { info "$1: $n red pixels"; return 0; }
    sleep 2
  done
  info "$1: only $n red pixels"; return 1
}

# within SECS CMD...: CMD succeeds within SECS seconds
within() {
  local end=$((SECONDS + $1)); shift
  until "$@"; do ((SECONDS < end)) || return 1; sleep 0.2; done
}

# shown NAME: a window titled NAME is mapped on R's display (and so focused by matchbox)
shown() { inx "$XDOTOOL" search --onlyvisible --name "$1" >/dev/null 2>&1; }

# differs A.png B.png: number of pixels that differ
differs() { "$MAGICK" compare -metric AE "$1" "$2" null: 2>&1 | awk '{print int($1)}'; }

# mostly NAME COLOUR: within 8 s a screenshot NAME is at least half COLOUR (matchbox fits every window to the screen)
mostly() {
  local end=$((SECONDS + 8)) n=0
  while ((SECONDS < end)); do
    n=$(pixels "$(shot "$1")" "$2")
    ((n >= TW * TH / 2)) && return 0
    sleep 0.5
  done
  info "$1: only $n $2 pixels"; return 1
}

# procs PATTERN: our processes whose command line matches PATTERN, except this script and its ancestors
# (the shell that started the test may mention the runtime dir)
procs() {
  local skip=" " p=$$ pid
  while ((p > 1)); do skip+="$p "; p=$(ps -o ppid= -p "$p") || break; p=${p// /}; done
  for pid in $(pgrep -u "$UID" -f "$1"); do [[ $skip == *" $pid "* ]] || echo "$pid"; done
}

# show_procs PATTERN: list them, indented
show_procs() { local pids; pids=$(procs "$1" | paste -sd,); [[ -z $pids ]] || ps -o pid=,args= -p "$pids" | sed 's/^/     left: /'; }

# gone PATTERN TIMEOUT: no process of ours matches PATTERN within TIMEOUT seconds
gone() {
  local end=$((SECONDS + $2))
  while ((SECONDS < end)); do [[ -z $(procs "$1") ]] && return 0; sleep 0.2; done
  show_procs "$1"; return 1
}

step1() {
  round a -v -- "$XTERM" -bg red -fa Monospace -fs 11
  check "1: session started" wait_session a || return
  check "1: red xterm fills >= 60% of the text area" wait_red s1-fill
  check "1: X screen is the $(expect 1000 700) text area" wait_xsize "$(expect 1000 700)" 5
}

step3() {
  local w; w=$("$H" wid)
  onhost "$XDOTOOL" windowsize "$w" 1400 900
  check "3: resize to 1400x900 -> $(expect 1400 900)" wait_xsize "$(expect 1400 900)" 5
  shot s3-1400x900 >/dev/null
  onhost "$XDOTOOL" windowsize "$w" 700 500; check "3: resize to 700x500 -> $(expect 700 500)" wait_xsize "$(expect 700 500)" 5
  shot s3-700x500 >/dev/null
  onhost "$XDOTOOL" windowsize "$w" 1000 700
  check "3: back to 1000x700 -> $(expect 1000 700)" wait_xsize "$(expect 1000 700)" 5
}

step4() {
  inx "$GWF" >"$OUT/gwf.log" 2>&1 &
  local gwf=$!
  within 30 shown widget-factory || info "4: widget factory window not seen"
  sleep 2
  "$H" xmove 100 100; sleep 3; shot s4-pointer-away >/dev/null
  "$MAGICK" "$OUT/s4-pointer-away.png" -crop 40x40+490+340 +repage "$OUT/s4-crop-away.png"
  "$H" xmove 500 350
  check "4: a pointer is drawn over the GTK window" within 6 pointer_drawn
  kill "$gwf"; wait "$gwf" 2>/dev/null
}

# pointer_drawn: the area around 500,350 differs from s4-crop-away by more than 20 pixels (an arrow is ~70)
pointer_drawn() {
  "$MAGICK" "$(shot s4-pointer-here)" -crop 40x40+490+340 +repage "$OUT/s4-crop-here.png"
  local n; n=$(differs "$OUT/s4-crop-here.png" "$OUT/s4-crop-away.png")
  info "4: $n pixels differ around the pointer position"
  ((n > 20))
}

# stacking: ids of the top-level windows, topmost first (matchbox does not update _NET_ACTIVE_WINDOW on next/prev)
stacking() { inx "$XWININFO" -root -children | sed -n 's/^ *\(0x[0-9a-f]*\) .*/\1/p' | paste -sd' '; }

step5() {
  inx "$XCLOCK" >"$OUT/xclock.log" 2>&1 &
  local clock=$! first after_n after_p
  within 20 shown xclock || info "5: xclock window not seen"
  sleep 1
  first=$(stacking)
  check "5: xclock is shown on top of the red xterm" mostly s5-xclock white
  "$H" xkey alt+shift+n; sleep 1; after_n=$(stacking)
  check "5: alt+shift+n shows the xterm" mostly s5-alt-shift-n red
  "$H" xkey alt+shift+p; sleep 1; after_p=$(stacking)
  check "5: alt+shift+p shows xclock again" mostly s5-alt-shift-p white
  info "5: stacking [$first] -> alt+shift+n [$after_n] -> alt+shift+p [$after_p]"
  check "5: alt+shift+n raises another window" test -n "$first" -a "$first" != "$after_n"
  check "5: alt+shift+p restores the order" test "$after_p" = "$first"
  # xpra mirrors kitty's NumLock into the session; matchbox must ignore it when matching its bindings
  "$H" xkey Num_Lock; sleep 1
  info "5: NumLock in the session: $(inx "$XSET" q | sed -n 's/.*\(Num Lock: *[a-z]*\).*/\1/p')"
  "$H" xkey alt+shift+n; sleep 1
  check "5: alt+shift+n raises another window with NumLock on" test "$(stacking)" != "$first"
  "$H" xkey alt+shift+p; sleep 1
  check "5: alt+shift+p restores the order with NumLock on" test "$(stacking)" = "$first"
  "$H" xkey Num_Lock; sleep 1
  kill "$clock"; wait "$clock" 2>/dev/null
}

# xev logged a KeyPress of F11 with Shift held
xev_saw_shift_f11() { grep -A2 KeyPress "$OUT/xev.log" | grep -q 'state 0x1, keycode [0-9]* (keysym 0xffc8, F11)'; }

step6() {
  inx "$XEV" -event keyboard >"$OUT/xev.log" 2>&1 &
  within 20 shown 'Event Tester' || info "6: xev window not seen"
  sleep 1
  "$H" xkey shift+F11
  check "6: Shift+F11 reaches the X program" within 5 xev_saw_shift_f11
  "$H" xkey alt+shift+F4
  local rc; rc=$(wait_rc a 15)
  check "6: Alt+Shift+F4 detaches with status 0 (got '$rc')" test "$rc" = 0
  check "6: tty is cooked again, no staircase" tty_cooked 2
  shot s6-after-detach >/dev/null
  check "6: session processes gone" gone "$RT/a/" 10
}

step2() {
  round b -- "$XTERM" -e sh -c "read l; echo \"\$l\" > $RT/typed; sleep 2"
  check "2: session started" wait_session b || return
  "$H" xtype e2e-ok; "$H" xkey Return
  local rc; rc=$(wait_rc b 30)
  check "2: typed text reached the program" grep -qx e2e-ok "$RT/typed"
  check "2: tg exit status 0 (got '$rc')" test "$rc" = 0
}

step7() {
  round c -- "$XCLOCK" -update 1
  check "7: session started" wait_session c || return
  kill -TERM "$(app_pid "$R")"
  local rc; rc=$(wait_rc c 5)
  check "7: killing the program ends tg with 143 (got '$rc')" test "$rc" = 143
  check "7: tty is cooked again, no staircase" tty_cooked 7
  shot s7-after-app-exit >/dev/null
  check "7: session processes gone" gone "$RT/c/" 10
}

step8() {
  round d -- "$XCLOCK" -update 1
  check "8: session started" wait_session d || return
  pkill -KILL -u "$UID" -f "attach socket://$R/"
  local rc f; rc=$(wait_rc d 15)
  check "8: client crash ends tg with 4 (got '$rc')" test "$rc" = 4
  check "8: prompt usable" tty_cooked 8
  f=$(shot s8-after-crash)
  check "8: no frozen xclock image (white pixels < 2%)" test "$(pixels "$f" white)" -lt $((TW * TH / 50))
  check "8: session processes gone" gone "$RT/d/" 10
}

step9() {
  round e -- "$XCLOCK" -update 1
  check "9: session started" wait_session e || return
  check "9: SIGHUP sent to tg" kill -HUP "$(cut -d" " -f1 "$R/tg.pid")"
  check "9: SIGHUP to tg leaves nothing running within 10 s" gone "$RT/e/" 10
  check "9: tty is cooked again" tty_cooked 9
}

step10() {
  KH_DIR=$KH_DIR2 "$H" start >/dev/null || { fail "10: second kitty started"; return; }
  round f -- "$XTERM" -bg red
  KH_DIR=$KH_DIR2 round g -- "$XTERM" -bg red
  check "10: first session started" wait_session f || return
  local r1=$R d1; d1=$(display_of "$R")
  check "10: first session renders" wait_red s10-first
  check "10: second session started" wait_session g || return
  KH_DIR=$KH_DIR2 check "10: second session renders" wait_red s10-second
  check "10: distinct displays ($d1, $(display_of "$R"))" test "$d1" != "$(display_of "$R")"
  KH_DIR=$KH_DIR2 "$H" stop >/dev/null
  check "10: closing the second kitty window ends its session" gone "$RT/g/" 15
  check "10: first session still alive" test -e "$r1/meta.json" -a -S "$r1/x.sock"
  "$H" xkey alt+shift+F4
  check "10: first session detaches" gone "$RT/f/" 15
}

# popup_shown NAME: within 6 s the screen differs from s11-gtk by more than 2000 pixels (the pointer alone is ~70).
# Polled: at load 90 a mapped menu once took longer than 1.5 s to reach the screen.
popup_shown() {
  local end=$((SECONDS + 6)) n=0
  while ((SECONDS < end)); do
    n=$(differs "$OUT/s11-gtk.png" "$(shot "$1")")
    ((n > 2000)) && { info "$1: $n pixels differ"; return 0; }
    sleep 0.5
  done
  info "$1: only $n pixels differ"; return 1
}

# no_popup: the screen is back to s11-gtk (a click while a popup is still open would only close it)
no_popup() { (($(differs "$OUT/s11-gtk.png" "$(shot s11-now)") < 2000)); }

# popup NAME X Y [BUTTON]: clicking at X,Y (kitty window pixels) opens a popup; Escape closes it again
popup() {
  within 6 no_popup || info "$1: the previous popup is still shown"
  "$H" xclick "$2" "$3" "${4:-1}"
  check "11: $1 opens" popup_shown "$1"
  "$H" xkey Escape; sleep 1
}

step11() {
  round h -- "$GWF"
  check "11: session started" wait_session h || return
  within 30 shown widget-factory || info "11: widget factory window not seen"
  sleep 2
  shot s11-gtk >/dev/null
  popup s11-context-menu 100 260 3
  popup s11-combo 448 260
  popup s11-header-menu 843 35
  "$H" xkey alt+shift+F4
  local rc; rc=$(wait_rc h 15)
  check "11: Alt+Shift+F4 detaches with status 0 (got '$rc')" test "$rc" = 0
  check "11: tty is cooked again" tty_cooked 11
  check "11: session processes gone" gone "$RT/h/" 10
}

cleanup() {
  KH_DIR=$KH_DIR2 "$H" stop >/dev/null 2>&1
  "$H" stop >/dev/null 2>&1
  local pids
  mapfile -t pids < <(procs "$RT/")
  ((${#pids[@]})) || return 0
  kill -TERM "${pids[@]}" 2>/dev/null; sleep 1; kill -KILL "${pids[@]}" 2>/dev/null
}

home_state() {
  sha256sum ~/.Xauthority 2>/dev/null
  stat -c '%n %Y %s' ~/.config/xpra ~/.xpra /tmp/xpra 2>/dev/null
}

main() {
  [[ -x $TG ]] || { echo "TG=$TG is not executable"; exit 2; }
  rm -rf "$RT" && (umask 077 && mkdir -p "$RT" "$OUT")
  XTERM=$(nixbin xterm xterm) XCLOCK=$(nixbin xclock xclock) XRANDR=$(nixbin xrandr xrandr) XEV=$(nixbin xev xev)
  XDOTOOL=$(nixbin xdotool xdotool) MAGICK=$(nixbin imagemagick magick) GWF=$(nixbin gtk3.dev gtk3-widget-factory)
  XWININFO=$(nixbin xwininfo xwininfo) XSET=$(nixbin xset xset)
  local before shm_before t
  before=$(home_state) shm_before=$(ls /dev/shm/xpra-terminal-* 2>/dev/null)
  trap cleanup EXIT
  "$H" start >/dev/null || { echo "FAIL kitty harness did not start"; exit 1; }
  sleep 2
  measure_cells || { echo "FAIL could not measure the kitty cell size"; exit 1; }
  local steps=("$@")
  ((${#steps[@]})) || steps=(1 3 4 5 6 2 7 8 9 10 11)
  for t in "${steps[@]}"; do echo "== step $t"; "step$t"; done
  cleanup
  check "final: nothing of ours left running" gone "$RT/" 10
  check "final: no new /dev/shm/xpra-terminal-* objects" test "$(ls /dev/shm/xpra-terminal-* 2>/dev/null)" = "$shm_before"
  check "final: ~/.Xauthority, ~/.config/xpra, ~/.xpra, /tmp/xpra unchanged" test "$(home_state)" = "$before"
  info "global xpra/X/matchbox processes (may include other users of this machine):"
  pgrep -a -u "$UID" -f 'Xorg-for-Xpra|Xvfb-for-Xpra|xpra (start|attach)|matchbox' | sed 's/^/       /'
  echo "== $PASSES passed, $FAILS failed (screenshots in $OUT)"
  ((FAILS == 0))
}

main "$@"
