#!/usr/bin/env bash
# Fake-kitty smoke test for tg (no kitty, no GL, no X display needed).
#
# usage: TG=/path/to/tg [TG_SMOKE_DIR=DIR] tests/fakekitty/run.sh [TEST...]
#   TG              tg to test, e.g. the dev-linked one from tests/dev-link.sh (required)
#   TG_SMOKE_DIR    scratch parent, wiped at start; must be short (AF_UNIX) (default /tmp/tg-smoke-$UID).
#                   Not TG_RUNTIME_DIR: the suite deletes it and kills every process that mentions it.
#   TEST            any of 1 2 3 4 5 6 7 8 (default: all)
#   RESIZE_AT       seconds after start when the fake terminal shrinks (default 8; raise it on a loaded host)
#
# Tests: 1 desktop/xdummy size, resize, exit status, frames, logs   2 client killed -> exit 4
#        3 SIGTERM / SIGKILL to tg, then --gc                           4 seamless and xvfb variants of 1
#        5 refusals start nothing                                     6 quick-exit hint
#        7 Alt+Shift+F4 detach -> 0, dir removed                          8 client fails at startup -> 4, promptly
set -uo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
: "${TG:?set TG to the tg under test}"
BASE=${TG_SMOKE_DIR:-/tmp/tg-smoke-$UID}
[[ -z ${TG_RUNTIME_DIR:-} || $BASE != "$TG_RUNTIME_DIR" ]] ||
  { echo "refusing to wipe TG_RUNTIME_DIR=$TG_RUNTIME_DIR; set TG_SMOKE_DIR instead"; exit 2; }
unset TG_RUNTIME_DIR
RESIZE_AT=${RESIZE_AT:-8}
PASSES=0 FAILS=0

pass() { PASSES=$((PASSES + 1)); echo "PASS $*"; }
fail() { FAILS=$((FAILS + 1)); echo "FAIL $*"; }
# check NAME CMD...: PASS/FAIL on CMD's status
check() { local name=$1; shift; if "$@"; then pass "$name"; else fail "$name"; fi; }
info() { echo "     $*"; }

# nixbin PKG BIN: a tool from nixpkgs as locked by this repo's flake
nixbin() { echo "$(nix build --no-link --print-out-paths --inputs-from "$REPO" "nixpkgs#$1" | head -1)/bin/$2"; }

# fk OUT CMD...: run CMD under the fake kitty, as if typed into a kitty window; returns CMD's status
fk() {
  local out=$1; shift
  python3 -I "$HERE/fakekitty.py" "$out" env -u TMUX -u STY TERM=xterm-kitty KITTY_WINDOW_ID=1 "$@"
}

# meta_ready META_JSON: tg has recorded the display and launched the program
meta_ready() {
  python3 -I -c 'import json,sys; m=json.load(open(sys.argv[1])); sys.exit(not (m["display"] and m["pids"]["app"]))' \
    "$1" 2>/dev/null
}

# session_dir PARENT TIMEOUT: print the session dir under PARENT once tg has launched the program
session_dir() {
  local end=$((SECONDS + $2)) m
  while ((SECONDS < end)); do
    for m in "$1"/*/meta.json; do meta_ready "$m" && { dirname "$m"; return 0; }; done
    sleep 0.2
  done
  return 1
}

display_of() { python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["display"])' "$1/meta.json"; }

# xsize R: the X screen size of session R as "WxH"
xsize() {
  XAUTHORITY=$1/Xauthority "$XRANDR" -display "$(display_of "$1")" -q 2>/dev/null \
    | sed -n 's/.*current \([0-9]*\) x \([0-9]*\).*/\1x\2/p'
}

# wait_xsize R WxH TIMEOUT
wait_xsize() {
  local end=$((SECONDS + $3))
  while ((SECONDS < end)); do [[ $(xsize "$1") == "$2" ]] && return 0; sleep 0.2; done
  return 1
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

# ours [DIR]: some process mentions DIR (default BASE) on its command line, i.e. a session under it is alive
ours() { [[ -n $(procs "${1:-$BASE/}") ]]; }

no_leftovers() {
  local name=$1 dpy=${2:-} end=$((SECONDS + 10))
  while ours && ((SECONDS < end)); do sleep 0.2; done
  if ours; then fail "$name: no leftover processes"; show_procs "$BASE/"
  else pass "$name: no leftover processes"; fi
  check "$name: no new /dev/shm/xpra-terminal-* objects" \
    test "$(shm_objects)" == "$SHM_BEFORE"
  [[ -n $dpy ]] && check "$name: X socket for $dpy removed" test ! -e "/tmp/.X11-unix/X${dpy#:}"
}

shm_objects() { find /dev/shm -maxdepth 1 -name 'xpra-terminal-*' -user "$UID" 2>/dev/null | sort; }

# has_seq FILE SEQ...: FILE contains every SEQ
has_seq() {
  local f=$1 s; shift
  for s in "$@"; do grep -qaF -- "$s" "$f" || return 1; done
}

nothing_running() { ! ours "$@"; }

# frames OUT: an inline kitty image in OUT decodes to width*height pixels
frames() { python3 -I "$HERE/frames.py" "$1" | sed 's/^/     /'; return "${PIPESTATUS[0]}"; }

client_log_nonempty() { local f; for f in "$1"/xpra/xpra-terminal-*.log; do [[ -s $f ]] && return 0; done; return 1; }

# Runs APP under tg in the fake kitty, with a resize at RESIZE_AT, and checks sizes, status, tty output and logs.
# session NAME TG_OPTS ENV INITIAL_SIZE APP...   (INITIAL_SIZE "-" means: only report it)
session() {
  local name=$1 opts=$2 envs=$3 initial=$4; shift 4
  local rt=$BASE/$name out=$BASE/$name.bin err=$BASE/$name.err t0=$SECONDS R dpy rc
  mkdir -m 700 "$rt"
  # shellcheck disable=SC2086  # opts and envs are word lists
  DUR=$((RESIZE_AT + 30)) RESIZE=100,30,1000,600@$RESIZE_AT fk "$out" env TG_RUNTIME_DIR="$rt" $envs \
    "$TG" $opts -- "$@" 2>"$err" &
  local fkpid=$!
  if ! R=$(session_dir "$rt" 90); then
    fail "$name: session became ready"; wait "$fkpid"; return
  fi
  dpy=$(display_of "$R")
  info "$name: ready after $((SECONDS - t0)) s on $dpy, X size $(xsize "$R")"
  if [[ $initial != - ]]; then
    if ((SECONDS - t0 < RESIZE_AT)); then
      check "$name: initial X size $initial" wait_xsize "$R" "$initial" $((RESIZE_AT - (SECONDS - t0) + 1))
    else
      fail "$name: ready only after the resize at ${RESIZE_AT}s; rerun with a larger RESIZE_AT"
    fi
  fi
  while ((SECONDS - t0 < RESIZE_AT)); do sleep 0.2; done
  if [[ $initial == - ]]; then
    wait_xsize "$R" 1000x600 6
    info "$name: X size after resize $(xsize "$R")"
  else
    check "$name: X size follows resize to 1000x600" wait_xsize "$R" 1000x600 6
  fi
  wait "$fkpid"; rc=$?
  check "$name: tg exit status $rc == 7" test "$rc" = 7
  check "$name: alternate screen entered and left" has_seq "$out" $'\e[?1049h' $'\e[?1049l'
  check "$name: kitty keyboard flags popped" has_seq "$out" $'\e[<u'
  check "$name: an inline frame decodes" frames "$out"
  if grep -qE "(display :[0-9]+ size|monitor 0 is .*) 1920x1080" "$R/server.log"; then
    fail "$name: no 1920x1080 phase in server.log"
  else pass "$name: no 1920x1080 phase in server.log"; fi
  info "$name: UnboundLocalError lines in server.log: $(grep -c UnboundLocalError "$R/server.log")"
  local f
  for f in server.log tg.log; do check "$name: $f non-empty" test -s "$R/$f"; done
  [[ $envs != *xvfb* ]] && check "$name: Xorg.log non-empty" test -s "$R/Xorg.log"
  check "$name: app.log exists" test -e "$R/app.log"
  check "$name: client log non-empty" client_log_nonempty "$R"
  no_leftovers "$name" "$dpy"
}

# xterm's own exit status is always 0, so a shell owns the status and closes the xterm 4 s after the resize
# shellcheck disable=SC2016  # expanded by the inner sh
APP_EXIT_7='"$0" -e sleep 600 & sleep "$1"; kill $!; exit 7'

test1() {
  session t1 -v "" 1200x800 sh -c "$APP_EXIT_7" "$XTERM" $((RESIZE_AT + 4))
}

test2() {
  local rt=$BASE/t2 out=$BASE/t2.bin R rc
  mkdir -m 700 "$rt"
  DUR=40 fk "$out" env TG_RUNTIME_DIR="$rt" "$TG" -- "$XCLOCK" -update 1 &
  local fkpid=$!
  if ! R=$(session_dir "$rt" 90); then fail "t2: session became ready"; wait "$fkpid"; return; fi
  local end=$((SECONDS + 30))
  until compgen -G "$R/xpra/xpra-terminal-*.log" >/dev/null || ((SECONDS > end)); do sleep 0.2; done
  sleep 2
  pkill -TERM -u "$UID" -f "attach socket://$R/"
  wait "$fkpid"; rc=$?
  check "t2: tg exit status $rc == 4 after the client was killed" test "$rc" = 4
  check "t2: session dir kept" test -d "$R"
  check "t2: message names the logs" has_seq "$out" "$R"
  check "t2: alternate screen left" has_seq "$out" $'\e[?1049l'
  no_leftovers t2
  rm -rf "$R"
}

test3() {
  local rt=$BASE/t3 out=$BASE/t3.bin R rc
  mkdir -m 700 "$rt"
  DUR=40 fk "$out" env TG_RUNTIME_DIR="$rt" "$TG" -- "$XCLOCK" -update 1 &
  local fkpid=$!
  if ! R=$(session_dir "$rt" 90); then fail "t3: session became ready"; wait "$fkpid"; return; fi
  sleep 3
  check "t3: SIGTERM sent to tg" kill -TERM "$(cut -d" " -f1 "$R/tg.pid")"
  wait "$fkpid"; rc=$?
  check "t3: tg exit status $rc == 143 after SIGTERM" test "$rc" = 143
  check "t3: session dir kept" test -d "$R"
  no_leftovers t3
  TG_RUNTIME_DIR=$rt "$TG" --gc </dev/null | sed 's/^/     /'
  check "t3: --gc removed the session dir" test ! -e "$R"

  # SIGKILL: tg cannot clean up; the client gets the hangup when the terminal goes away, the server follows it
  # (--exit-with-client), and --gc removes what is left
  DUR=40 fk "$out" env TG_RUNTIME_DIR="$rt" "$TG" -- "$XCLOCK" -update 1 &
  fkpid=$!
  if ! R=$(session_dir "$rt" 90); then fail "t3k: session became ready"; wait "$fkpid"; return; fi
  local dpy
  dpy=$(display_of "$R")
  sleep 3
  check "t3k: SIGKILL sent to tg" kill -KILL "$(cut -d" " -f1 "$R/tg.pid")"
  wait "$fkpid"
  TG_RUNTIME_DIR=$rt "$TG" --gc </dev/null | sed 's/^/     /'
  check "t3k: --gc removed the session dir after kill -9" test ! -e "$R"
  no_leftovers t3k "$dpy"
}

test4() {
  session t4s -s "" - sh -c "$APP_EXIT_7" "$XTERM" $((RESIZE_AT + 4))
  # Xvfb starts at 7680x4320 and is only re-fitted after the handshake; the first full-screen frame travels
  # inline (no shm here) and can take many seconds on a loaded host
  local at=$((RESIZE_AT + 15))
  RESIZE_AT=$at session t4x "" TG_XSERVER=xvfb 1200x800 sh -c "$APP_EXIT_7" "$XTERM" $((at + 4))
}

# refuse NAME EXPECTED_TEXT CMD...: CMD must exit 2, print EXPECTED_TEXT, and start nothing.
# CMD writes to $BASE/NAME.log; fake-kitty runs record the tty in $BASE/NAME.bin.
refuse() {
  local name=$1 text=$2 rc; shift 2
  "$@" >"$BASE/$name.log" 2>&1; rc=$?
  check "$name: exit status $rc == 2" test "$rc" = 2
  check "$name: says '$text'" grep -qaF -- "$text" "$BASE/$name.log" "$BASE/$name.bin"
  check "$name: no session dir" test -z "$(find "$BASE/t5" -mindepth 1 -maxdepth 2 -type d 2>/dev/null)"
  check "$name: nothing started" nothing_running
}

test5() {
  local long rt=$BASE/t5
  long=/tmp/tg-smoke-$UID-$(printf 'x%.0s' $(seq 110))
  mkdir -m 700 "$rt"
  refuse t5-tmux "inside tmux" fk "$BASE/t5-tmux.bin" env TG_RUNTIME_DIR="$rt" TMUX=x "$TG" "$XTERM"
  refuse t5-screen "inside screen" fk "$BASE/t5-screen.bin" env TG_RUNTIME_DIR="$rt" STY=x "$TG" "$XTERM"
  refuse t5-notkitty "does not look like kitty" fk "$BASE/t5-notkitty.bin" \
    env -u KITTY_WINDOW_ID TG_RUNTIME_DIR="$rt" TERM=xterm-256color "$TG" "$XTERM"
  refuse t5-notty "must be the kitty terminal" env TG_RUNTIME_DIR="$rt" "$TG" "$XTERM" </dev/null
  refuse t5-nocmd "no COMMAND" fk "$BASE/t5-nocmd.bin" env TG_RUNTIME_DIR="$rt" "$TG"
  refuse t5-notfound "command not found" fk "$BASE/t5-notfound.bin" \
    env TG_RUNTIME_DIR="$rt" "$TG" no-such-command-xyz
  refuse t5-longdir "too long" fk "$BASE/t5-longdir.bin" env TG_RUNTIME_DIR="$long" "$TG" "$XTERM"
  check "t5-longdir: nothing started under the long path" nothing_running "$long"
  check "t5-longdir: no session dir under the long path" \
    test -z "$(find "$long" -mindepth 1 -maxdepth 1 -type d 2>/dev/null)"
  rm -rf "$long"
  mkdir -m 755 "$BASE/t5-mode"
  refuse t5-mode "expected 700" fk "$BASE/t5-mode.bin" env TG_RUNTIME_DIR="$BASE/t5-mode" "$TG" "$XTERM"
}

test6() {
  local rt=$BASE/t6 out=$BASE/t6.bin rc
  mkdir -m 700 "$rt"
  DUR=90 fk "$out" env TG_RUNTIME_DIR="$rt" "$TG" -- true; rc=$?
  check "t6: tg exit status $rc == 0" test "$rc" = 0
  check "t6: hint 'exited after'" has_seq "$out" "exited after"
  check "t6: hint 'running instance'" has_seq "$out" "running instance"
  check "t6: session dir removed" test -z "$(find "$rt" -mindepth 1 -maxdepth 1 -type d)"
  no_leftovers t6
}

# kitty's encoding (keyboard protocol flags 31) of Alt+Shift+F4 press and release
DETACH_KEY='\x1b[1;4S\x1b[1;4:3S'

test7() {
  local rt=$BASE/t7 out=$BASE/t7.bin R rc
  mkdir -m 700 "$rt"
  DUR=60 INPUT="$DETACH_KEY@$((RESIZE_AT + 12))" fk "$out" env TG_RUNTIME_DIR="$rt" "$TG" -- "$XTERM" &
  local fkpid=$!
  if ! R=$(session_dir "$rt" 90); then fail "t7: session became ready"; wait "$fkpid"; return; fi
  wait "$fkpid"; rc=$?
  check "t7: tg exit status $rc == 0 after Alt+Shift+F4" test "$rc" = 0
  check "t7: alternate screen left, keyboard flags popped" has_seq "$out" $'\e[?1049l' $'\e[<u'
  check "t7: session dir removed" test ! -e "$R"
  no_leftovers t7
}

# a client that exits before its hello: the server gets no exit-with-client, tg must stop it with `xpra stop`
test8() {
  local rt=$BASE/t8 out=$BASE/t8.bin rc t0 tc R
  mkdir -m 700 "$rt"
  t0=$SECONDS
  DUR=60 fk "$out" env TG_RUNTIME_DIR="$rt" TG_CLIENT_ARGS=--bogus-option "$TG" -- "$XCLOCK" -update 1; rc=$?
  R=$(dirname "$(compgen -G "$rt/*/tg.log" | head -1)")
  check "t8: tg exit status $rc == 4 after a client startup failure" test "$rc" = 4
  tc=$(python3 -I - "$R/tg.log" <<'EOF'
import re, sys
t = [sum(float(x) * m for x, m in zip(l.split()[0].split(":"), (3600, 60, 1))) for l in open(sys.argv[1])
     if re.search(r"client pid|exit \d+$", l)]
print(f"{t[-1] - t[0]:.1f}" if len(t) >= 2 else "?")
EOF
)
  info "t8: tg returned $tc s after starting the client ($((SECONDS - t0)) s in all)"
  check "t8: tg returned within 4 s of the client's start" python3 -I -c "import sys; sys.exit(not float(sys.argv[1]) < 4)" "$tc"
  check "t8: message names the logs" has_seq "$out" "xpra client failed"
  no_leftovers t8
  [[ -n $R && $R != . ]] && rm -rf "$R"
}

cleanup() {
  local pids
  mapfile -t pids < <(procs "$BASE/")
  ((${#pids[@]})) || return 0
  kill -TERM "${pids[@]}" 2>/dev/null; sleep 1; kill -KILL "${pids[@]}" 2>/dev/null
}

main() {
  [[ -x $TG ]] || { echo "TG=$TG is not executable"; exit 2; }
  rm -rf "$BASE" && (umask 077 && mkdir -p "$BASE")
  XRANDR=$(nixbin xrandr xrandr) XTERM=$(nixbin xterm xterm) XCLOCK=$(nixbin xclock xclock)
  SHM_BEFORE=$(shm_objects)
  trap cleanup EXIT
  local tests=("$@") t
  ((${#tests[@]})) || tests=(1 2 3 4 5 6 7 8)
  for t in "${tests[@]}"; do echo "== test $t"; "test$t"; done
  echo "== $PASSES passed, $FAILS failed (outputs in $BASE)"
  ((FAILS == 0))
}

main "$@"
