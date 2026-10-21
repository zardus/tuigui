# tuigui (`tg`)

`tg` runs an X11 GUI program inside your kitty terminal. It starts a private X server and an
[xpra](https://github.com/Xpra-org/xpra) session, runs the program there, and draws it in the terminal with the kitty
graphics protocol. The program fills the terminal's text area, follows its size, and gets your keyboard and mouse.
When the program exits, you get your shell prompt back.

<!-- TODO: screenshot of Firefox filling a kitty window -->

Requirements: Linux, Nix with flakes enabled, and kitty (tested with 0.49.2). tmux and screen are not supported.

## Install

```sh
nix profile add github:zardus/tuigui   # first install compiles xpra (~5 min, ~1.8 GB download)
nix run github:zardus/tuigui -- xterm   # try it without installing
nix profile upgrade tuigui              # update
```

xpra is built from a pinned commit of its master branch, because only master has the terminal backend. That is why
the first install compiles it.

## Use

```sh
tg firefox --new-instance -P tg   # options after COMMAND belong to COMMAND
tg -- gimp image.png
tg -s xterm                       # seamless mode, see below
tg                                # list the xpra servers you can attach to
tg :100                           # attach to one of them
```

Keys inside the session:

| Key | Action |
|---|---|
| Alt+Shift+F4 | detach and quit (this also ends the program, except after `tg :N`) |
| Alt+Shift+n / Alt+Shift+p | next / previous window (dialogs, extra windows) |
| everything else, including Ctrl+C | goes to the program |

- Resizing the kitty window resizes the X screen, and the program is fitted to the new size.
- The mouse works as on a desktop: the X pointer (arrow, text cursor) is drawn into the picture, and right-click menus,
  drop-down lists and popovers open in place.
- Copy works: text the program copies to the X CLIPBOARD reaches your clipboard via OSC 52. Paste does not work the
  same way. kitty types the clipboard into the program as keystrokes (see [Known limitations](#known-limitations)).
- Firefox, Chromium and some Electron apps hand off to an instance that is already running on your desktop. The window
  then opens on the desktop and `tg` exits at once with a hint. Start a separate instance instead:
  `tg firefox --new-instance -P tg`.

## Options and environment

```
tg [-s] [-v] [--] COMMAND [ARGS...]
tg [-v] :DISPLAY
tg
tg --gc
tg --help | --version
```

Options are recognised only before COMMAND. The first non-option word or `--` ends them.

Without arguments, `tg` lists the running xpra servers you can attach to: those with a socket of yours in xpra's
default socket directories (`$XDG_RUNTIME_DIR/xpra`, `/run/xpra`, `~/.xpra`) that answer `xpra id`. `tg`'s own
sessions are not listed, because their client is already attached.

`tg :DISPLAY` attaches to one of those servers in the current kitty window, with the same client settings and keys
as a session `tg` starts. Alt+Shift+F4 only detaches: the server and its programs keep running, and `tg` says so. A
client already attached to that server is disconnected (xpra's default without `--sharing`). `-s` does not apply,
and neither do the `TG_XSERVER`, `TG_TIMEOUT` and `TG_SERVER_ARGS` variables. Exit status: 0 after a detach or when
the server shuts down, 2 if no server of yours answers on DISPLAY, 4 if the client failed.

| Option | Meaning |
|---|---|
| `-s`, `--seamless` | Seamless mode: no window manager; each X window is drawn at its own X position. |
| `-v`, `--verbose` | Keep the session directory and logs on success and print its path. `-vv` also enables xpra client debug logging. |
| `-V`, `--version` | Print the tg and xpra versions. |
| `-h`, `--help` | Help. |
| `--gc` | Kill leftovers of sessions whose `tg` process is gone and remove their directories. |

| Variable | Default | Meaning |
|---|---|---|
| `TG_XSERVER` | `xdummy` | `xdummy` (Xorg with the dummy driver) or `xvfb`. |
| `TG_RUNTIME_DIR` | `$XDG_RUNTIME_DIR/tg`, else `/tmp/tg-<uid>` | Parent of the per-session directories. Must be short (see below). |
| `TG_TIMEOUT` | `60` | Seconds to wait for the X display and the xpra server. |
| `TG_FORCE_TERMINAL` | unset | `1` skips the kitty check. xpra still probes the terminal for graphics support. |
| `TG_SERVER_ARGS`, `TG_CLIENT_ARGS` | empty | Extra xpra server / client arguments, appended last (the last value of an option wins). |
| `XPRA_TERMINAL_*` | xpra defaults | Passed to the xpra client unchanged, e.g. `XPRA_TERMINAL_SHM=0`. |

Exit status: COMMAND's own status (0 after a detach); 2 for usage or environment errors (not a tty, tmux, not kitty,
command not found, runtime directory unusable); 3 if the xpra or X server failed; 4 if the xpra client failed;
128+N if COMMAND or `tg` was killed by signal N.

## How it works

```
kitty
└─ your shell
   └─ tg
      ├─ xpra start-desktop        private X server (Xorg + dummy driver) and matchbox window manager
      ├─ COMMAND                   DISPLAY points at the private X server
      └─ xpra attach --backend=terminal   on your tty: draws frames, forwards keys, mouse and resizes
```

- **Desktop mode** (default): xpra serves the whole X screen as one desktop. The screen starts at the terminal's text
  area size in pixels and follows it on every resize. matchbox, without title bars, fits the program's window to the
  screen.
- **Seamless mode** (`-s`): no window manager. Each X window is drawn at its own position. Windows larger than the
  terminal are cropped, and covered windows cannot be reached.
- **X server**: Xorg with xpra's dummy driver can resize to any size up to 7680x4320. `TG_XSERVER=xvfb` uses Xvfb with
  a 7680x4320 screen instead.
- **Graphics**: frames go to kitty through shared memory (`/dev/shm`) when kitty and `tg` run on the same machine,
  otherwise as base64-encoded images in the terminal stream.
- **Isolation**: every session has its own directory (`$TG_RUNTIME_DIR/<id>`) holding the xpra socket, the
  Xauthority file and xpra's runtime and config directories. Nothing under `$HOME` is written. The X display number is
  picked by the X server, so several `tg` sessions can run at the same time. The host's xpra configuration
  (`/etc/xpra`, `/etc/xdg/xpra`, `~/.xpra`, `~/.config/xpra`) is not read. COMMAND gets your environment, minus
  `WAYLAND_DISPLAY`, plus `DISPLAY`, `XAUTHORITY` (the session's Xauthority file), `TG_SESSION` (the path of the
  session directory, where the logs below are written) and variables that make GTK, Qt, Firefox and SDL use X11
  instead of a host Wayland session.
- **Lifetime**: a session lives exactly as long as its `tg`. Whichever of COMMAND, the xpra client or the server ends
  first, `tg` stops the rest, restores the terminal, and removes the session directory. The directory is kept, and
  its path printed, if something failed or `-v` was given. COMMAND counts as running while any process in its process
  group is alive, so `sh -c 'xclock & exit 0'` keeps the session until xclock exits.

Logs in the session directory:

| File | Contents |
|---|---|
| `tg.log` | tg's own timeline: phases, pids, exit codes |
| `server.log` | xpra server and window manager output |
| `Xorg.log` | X server log |
| `app.log` | COMMAND's stdout and stderr |
| `xpra/xpra-terminal-<pid>.log` | xpra client log while it draws |

## tmux, ssh, containers

- **tmux and screen** do not pass the kitty graphics and keyboard protocols through, so `tg` refuses to run inside
  them. Open a plain kitty window or tab instead.
- **ssh**: works, but shared memory is unavailable across machines. Frames then travel inline in the terminal stream,
  which costs more CPU and bandwidth.
- **Docker**: the default 64 MB `/dev/shm` can fill up with large windows. Use `--shm-size=1g`, or set
  `XPRA_TERMINAL_SHM=0`.
- **Path length**: unix socket paths are limited to about 100 bytes. `tg` checks the length and falls back from
  `$XDG_RUNTIME_DIR/tg` to `/tmp/tg-<uid>`. If you set `TG_RUNTIME_DIR`, keep it short.

## Troubleshooting

- **A few xpra lines appear above the prompt** before the session starts and after it ends. This is xpra's banner and
  exit statistics. The "(press Control-C to detach)" line is wrong for `tg`: Ctrl+C goes to the program and
  Alt+Shift+F4 detaches.
- **"terminal does not support the kitty graphics protocol"**: you are not in kitty, or something between `tg` and kitty
  (tmux, an ssh jump with filtering) strips the protocol.
- **The terminal is garbled after a crash**: run `reset`.
- **Leftover processes or directories** after `tg` was killed with SIGKILL: run `tg --gc`.
- **The program opened on your normal desktop** and `tg` exited at once: see the new-instance flags under [Use](#use).
- **Alt+Shift shortcuts do nothing** (some GNOME setups intercept them): use another modifier for the detach key,
  `TG_CLIENT_ARGS="--shortcut-modifiers=control+shift"`.
- **Anything else**: run with `-v` to keep the logs, or `-vv` to add client debug output, and read the files listed
  under [How it works](#how-it-works).

## Known limitations

- **Paste** into the program is kitty typing the clipboard as keystrokes. Newlines act as Enter, kitty asks for
  confirmation on control characters, non-ASCII text is mangled, and the X CLIPBOARD is never set. Copying out of the
  program works (OSC 52, needs kitty's default `clipboard_control`).
- **Keyboard**: non-ASCII input loses its high bit (`é` arrives as a different character). The layout is fixed to
  `pc105/us`, with no keymap sync and no input methods.
- **Startup noise**: xpra prints its banner (about 20 lines, including a red `querying drm devices` warning) before
  the session, and exit statistics after it. They stay in the scrollback.
- **Getting the prompt back**: the terminal is restored as soon as the program exits, but `tg` returns only after the
  X server has stopped. That takes about 1 s, and was seen to take 8 s at a load average of 38.
- **Detach ends the program.** There is no reattach. Closing the kitty window ends the session too.
- **Programs that hand off to a running instance** (Firefox, Chromium, some Electron apps) open on the host desktop.
  Use their new-instance or profile flags.
- **Programs that daemonize themselves** look as if they exited, and `tg` ends the session. A program that keeps
  running without a window is a problem in seamless mode, see below.
- **Host shortcuts** such as Alt+Tab never reach the session. GNOME hosts may intercept some Alt+Shift combinations.
- **No scaling**: the desktop is drawn 1:1 at 96 DPI. On HiDPI screens set `GDK_SCALE` or `QT_SCALE_FACTOR`; `tg`
  passes them through.
- **Seamless mode** crops windows larger than the terminal and cannot reach covered windows. Keyboard input,
  including the Alt+Shift+F4 detach key, only works while the program has a mapped window. With no window the session
  can only be ended by closing the kitty window or sending SIGTERM to `tg` from another shell.
- **Performance**: kitty's rendering is the bottleneck for large animated windows. Over ssh, or without a shared
  `/dev/shm`, frames are sent as base64. A small `/dev/shm` can fill up under load. `XPRA_TERMINAL_SHM=0` avoids shared
  memory, but the first frame may then stay black for a long time. On a heavily loaded machine (load average 90) a
  newly opened menu was seen to take more than 1.5 s to appear.
- **Xvfb** (`TG_XSERVER=xvfb`) cannot grow past 7680x4320. It also starts at that size: the first frame is large, and
  the screen is fitted to the terminal only once the client has connected.
- **OpenGL** inside the session is software rendering at best. Some programs need `LIBGL_ALWAYS_SOFTWARE=1`.
- **Resizing** takes 0.4-0.5 s, up to 1.4 s under load. The xpra server logs a harmless `UnboundLocalError` per resize
  on the pinned master.
- **Startup** takes 3-4 s on a quiet machine, and 10-40 s under heavy load.
- **Terminals**: only kitty is supported. Others are untested (`TG_FORCE_TERMINAL=1`), and tmux, screen and zellij
  are refused.
- **aarch64-linux** is declared in the flake but has never been built.

## Development

```
flake.nix, flake.lock       packages (tg, xpra, matchbox), apps, checks, devShell
nix/xpra.nix                nixpkgs' xpra, built from the pinned xpra-src input, no patches
nix/matchbox.nix            nixpkgs' matchbox, patched to ignore NumLock/CapsLock in its key bindings
nix/tg.nix                  installs src/tg.py with store paths substituted (no wrapper)
src/tg.py                   the whole program, Python 3 standard library only
src/share/matchbox-kbdconfig
tests/unit/                 unit tests of the pure functions (run by the package's checkPhase)
tests/dev-link.sh           a runnable tg from existing store paths, without building the flake
tests/fakekitty/            smoke test in a fake-kitty pty: no kitty or GPU needed
tests/kitty/                end-to-end test in a real kitty on an X display
```

```sh
nix develop                                  # xpra, ruff, xdotool, imagemagick, xterm, xrandr, xwininfo
nix flake check                              # unit tests, CLI checks, ruff
TG=$(tests/dev-link.sh) tests/fakekitty/run.sh  # scratch dir: TG_SMOKE_DIR (default /tmp/tg-smoke-$UID)
TG=$(tests/dev-link.sh) KH_DISPLAY=:N tests/kitty/e2e.sh  # scratch dir: TG_E2E_DIR (default /tmp/tg-e2e-$UID)
```

`tests/kitty/e2e.sh` needs a dedicated X display (an Xvfb, say) in `KH_DISPLAY`, never your desktop: it injects
XTEST input there. The test scripts wipe their scratch directories and kill the processes that mention them, so they
never use `TG_RUNTIME_DIR`. Flakes only see git-tracked files: `git add` new test files before `nix flake check`, or
run `python3 -m unittest discover -s tests/unit` directly against the working tree.

The three test tiers:

1. **Unit tests** (`tests/unit`) cover argument parsing, terminal checks, environments and the exact xpra command
   lines. They run in `nix flake check` and every package build.
2. **Fake-kitty smoke test** (`tests/fakekitty/run.sh`) runs real sessions in a pty that answers kitty's queries. It
   checks the X screen size and resize, exit statuses, detach with Alt+Shift+F4, decodable frames, logs, refusals,
   `--gc` after SIGTERM and SIGKILL, listing and attaching to a plain xpra server, and leftovers.
3. **Real kitty end-to-end test** (`tests/kitty/e2e.sh`) drives kitty with XTEST input. It checks the fill, typing,
   resize, the pointer, window switching, shortcuts and detach, app exit, a client crash, hangup, two concurrent
   sessions, and popup menus in a GTK program. Screenshots go to `tests/kitty/out/`.

The nixpkgs and xpra revisions are pinned in flake.lock (`nix flake metadata` shows them; xpra is a master commit,
currently in the 8.0 series). To update:

```sh
nix flake update xpra-src                    # current master, or pin a revision:
nix flake lock --override-input xpra-src github:Xpra-org/xpra/<rev>
nix flake update nixpkgs                     # separately, so breakage is attributable
nix build .#xpra -L                          # ~5 min
nix flake check
tests/fakekitty/run.sh && tests/kitty/e2e.sh
git commit flake.lock
```

Upstream xpra changes that would remove workarounds in `tg`:

- The terminal client should log to stderr or a log file instead of stdout, so nothing lands on the tty. The
  "(press Control-C to detach)" hint should go.
- Non-ASCII keys should use the kitty keyboard protocol's text as Unicode keysyms.
- Bracketed paste should set the remote CLIPBOARD instead of being typed as keystrokes.
- Fix the server's `UnboundLocalError 'wmm'` on resize, and the desktop-mode `xpra info` and `xpra screenshot`
  tracebacks.
- In seamless mode, draw a fallback arrow when the server sends the default cursor.
- The X11 server records the root cursor as the "default" during setup and never sends that shape. A window manager
  started with `--start` can set its arrow first, and the arrow is then invisible. `tg` starts matchbox with
  `--start-late` to avoid this.
- Add shared-memory backpressure, and degrade to patches instead of full base64 frames when `/dev/shm` is full.
- Clamp an Xvfb resize past its `-screen` size instead of failing with BadMatch.

## Credits and license

Built on [xpra](https://github.com/Xpra-org/xpra) and its kitty terminal backend (PR #5012),
[kitty](https://sw.kovidgoyal.net/kitty/) and matchbox-window-manager.

MIT, see [LICENSE](LICENSE).
