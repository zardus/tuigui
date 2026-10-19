#!@python3@ -I
"""tg: run an X11 GUI program inside the kitty terminal, via xpra's terminal backend."""
import base64
import collections
import fcntl
import glob
import json
import os
import secrets
import select
import shlex
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import termios
import time

VERSION = "@version@"
XPRA_PREFIX = "@xpra@"
XPRA = XPRA_PREFIX + "/bin/xpra"
XORG = "@xorgserver@/bin/Xorg"
XVFB = "@xorgserver@/bin/Xvfb"
MATCHBOX = "@matchbox@/bin/matchbox-window-manager"
SHARE = "@share@"

QUIET = ["--mdns=no", "--dbus-launch=no", "--dbus=no", "--dbus-control=no", "--notifications=no",
         "--pulseaudio=no", "--speaker=no", "--microphone=no", "--webcam=no", "--printing=no",
         "--file-transfer=no", "--open-files=no", "--open-url=no", "--systemd-run=no", "--html=off",
         "--http-scripts=off", "--input-method=none", "--start-via-proxy=no", "--source=", "--tray=no",
         "--xsettings=no", "--bell=no"]
CLIENT_QUIET = ["--speaker=no", "--microphone=no", "--audio=no", "--webcam=no", "--notifications=no",
                "--printing=no", "--file-transfer=no", "--open-files=no", "--open-url=no", "--tray=no", "--mdns=no"]
SHORTCUTS = ["--shortcut-modifiers=meta+shift", "--key-shortcut=clear", "--key-shortcut=#+F4:quit"]
X11_ENV = {"GDK_BACKEND": "x11", "QT_QPA_PLATFORM": "xcb", "MOZ_ENABLE_WAYLAND": "0",
           "SDL_VIDEODRIVER": "x11", "XDG_SESSION_TYPE": "x11"}
XPRA_SCRUB = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XPRA_CONF_DIR")
# Restores kitty after a client that died without cleaning up: main screen, keyboard protocol pop,
# mouse modes off, cursor on, all images deleted.
RESET_SEQ = (b"\x1b[?1049l\x1b[<u\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1016l"
             b"\x1b[?25h\x1b_Ga=d,d=A,q=2\x1b\\\r")
SOCKET_BUDGET = 100       # bytes for the longest AF_UNIX path xpra creates (the kernel limit is 107)
QUICK_EXIT = 3            # seconds; an app that exits sooner probably handed off to a running instance
TAIL_LINES = 20
CASCADE_GRACE = 0.5     # seconds; see Session.supervise

HELP = """\
tg - run an X11 GUI program inside this kitty terminal

usage: tg [-s] [-v] [--] COMMAND [ARGS...]
       tg --gc | --help | --version

tg starts a private X server and xpra session, runs COMMAND in it and shows
it in this terminal through the kitty graphics protocol. The program fills
the terminal and follows its size. tg exits when COMMAND exits, or when you
press Alt+Shift+F4 to detach, which also ends COMMAND.

options:
  -s, --seamless   draw each X window where it is instead of one fitted desktop
  -v, --verbose    keep logs on success and print their location (-vv: client debug)
  -V, --version    print versions
  -h, --help       this text
      --gc         clean up sessions whose tg process is gone

keys inside the session:
  Alt+Shift+F4             detach and quit
  Alt+Shift+n / Alt+Shift+p  next / previous window (dialogs, extra windows)
  everything else, including Ctrl+C, goes to the program

environment: TG_XSERVER=xdummy|xvfb  TG_RUNTIME_DIR=DIR  TG_TIMEOUT=SECS
             TG_FORCE_TERMINAL=1  TG_SERVER_ARGS=...  TG_CLIENT_ARGS=...

exit status: COMMAND's own; 2 usage/environment, 3 server failed, 4 client
failed, 128+N when interrupted by signal N.
Logs: $XDG_RUNTIME_DIR/tg/<id>/ (kept with -v or after a failure).
Does not work inside tmux or screen."""


class Usage(Exception):
    """A usage or environment problem: args are (cause, action); exit status 2."""


def err(cause, action=None):
    try:
        print(f"tg: {cause}", file=sys.stderr)
        if action:
            print(f"tg: {action}", file=sys.stderr)
    except OSError:          # the terminal is gone (hangup)
        pass


# ---------- pure functions (unit-tested) ----------

def parse_args(argv):
    """Returns (opts, cmd). Options stop at the first non-option or '--'."""
    opts = {"seamless": False, "verbose": 0, "gc": False, "help": False, "version": False}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            i += 1
            break
        if not a.startswith("-") or a == "-":
            break
        if a in ("-s", "--seamless"):
            opts["seamless"] = True
        elif a in ("-v", "--verbose"):
            opts["verbose"] += 1
        elif a == "-vv":
            opts["verbose"] += 2
        elif a in ("-h", "--help"):
            opts["help"] = True
        elif a in ("-V", "--version"):
            opts["version"] = True
        elif a == "--gc":
            opts["gc"] = True
        else:
            raise Usage(f"unknown option {a!r}", "see tg --help")
        i += 1
    cmd = argv[i:]
    if opts["help"] or opts["version"]:
        return opts, cmd
    if opts["gc"] and cmd:
        raise Usage("--gc takes no COMMAND", "see tg --help")
    if not opts["gc"] and not cmd:
        raise Usage("no COMMAND given", "see tg --help")
    return opts, cmd


def check_terminal(env, isatty0, isatty1):
    """Returns None or (cause, action)."""
    if not (isatty0 and isatty1):
        return ("stdin and stdout must be the kitty terminal", None)
    if env.get("TMUX") or env.get("STY"):
        mux = "tmux" if env.get("TMUX") else "screen"
        return (f"refusing to run inside {mux}: {mux} does not pass the kitty graphics and keyboard protocols through.",
                "open a plain kitty window or tab and run tg there.")
    if env.get("TG_FORCE_TERMINAL") == "1":
        return None
    if env.get("KITTY_WINDOW_ID") or "kitty" in env.get("TERM", ""):
        return None
    return ((f"this does not look like kitty (TERM={env.get('TERM', '')}); "
             "tg needs the kitty graphics and keyboard protocols."),
            "set TG_FORCE_TERMINAL=1 to try anyway (ghostty, WezTerm, konsole are untested).")


def runtime_bases(env, uid):
    """Every parent directory tg may use, in order of preference (--gc scans them all)."""
    out = []
    if env.get("TG_RUNTIME_DIR"):
        out.append(env["TG_RUNTIME_DIR"])
    if env.get("XDG_RUNTIME_DIR"):
        out.append(os.path.join(env["XDG_RUNTIME_DIR"], "tg"))
    out.append(f"/tmp/tg-{uid}")
    return out


def fits_af_unix(base, hostname):
    """Whether xpra's longest socket, $R/xpra/clients/<hostname>-<pid>, fits under base/<6 chars>."""
    return len(base) + 1 + 6 + len("/xpra/clients/") + len(hostname) + 8 <= SOCKET_BUDGET


def choose_base(env, uid, hostname):
    """The runtime base to use, or None. An explicit TG_RUNTIME_DIR is never silently replaced."""
    bases = runtime_bases(env, uid)
    if env.get("TG_RUNTIME_DIR"):
        bases = bases[:1]
    return next((b for b in bases if fits_af_unix(b, hostname)), None)


def term_size(fd):
    """(W, H) of the text area in pixels using the client's cell arithmetic, or None when unknown."""
    try:
        rows, cols, xpx, ypx = struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))
    except OSError:
        return None
    if not (rows and cols and xpx and ypx):
        return None
    return (xpx // cols) * cols, (ypx // rows) * rows


def xpra_env(env, R, with_xauth):
    e = {k: v for k, v in env.items() if k not in XPRA_SCRUB}
    e["XDG_RUNTIME_DIR"] = R
    e["XDG_CONFIG_HOME"] = os.path.join(R, "cfg")
    if with_xauth:
        e["XAUTHORITY"] = os.path.join(R, "Xauthority")
    return e


def app_env(env, R, display):
    e = {k: v for k, v in env.items() if k not in ("WAYLAND_DISPLAY", "XAUTHORITY")}
    e.update(X11_ENV, DISPLAY=display, XAUTHORITY=os.path.join(R, "Xauthority"), TG_SESSION=R)
    return e


def build_server_cmd(opts, R, w_fd, size, xserver, extra):
    sub = "start" if opts["seamless"] else "start-desktop"
    xa = os.path.join(R, "Xauthority")
    if xserver == "xvfb":
        xvfb = (f"{XVFB} -screen 0 7680x4320x24 +extension GLX +extension RANDR +extension RENDER "
                f"+extension Composite -extension DOUBLE-BUFFER -nolisten tcp -noreset -auth {xa}")
    else:
        xvfb = (f"{XORG} -noreset -novtswitch -nolisten tcp +extension GLX +extension RANDR +extension RENDER "
                f"+extension Composite -extension DOUBLE-BUFFER -auth {xa} -logfile {R}/Xorg.log "
                f"-configdir {R}/xorg.conf.d -config {XPRA_PREFIX}/etc/xpra/xorg.conf")
    cmd = [XPRA, sub, "--daemon=no", f"--displayfd={w_fd}",
           f"--bind={R}/x.sock", f"--socket-dirs={R}", f"--socket-dir={R}", f"--sessions-dir={R}/s",
           f"--log-dir={R}", "--exit-with-client=yes", "--exit-with-children=no", "--start-new-commands=no",
           "--server-idle-timeout=300", f"--xvfb={xvfb}"]
    if not opts["seamless"]:
        # initial resolutions are a dummy-driver feature; Xvfb starts at its -screen size and is re-fitted
        rd = "yes" if (size is None or xserver == "xvfb") else f"yes:{size[0]}x{size[1]}"
        # --start-late: xpra records the root cursor at setup as "default" and never draws it; with --start,
        # matchbox could set its arrow first and the arrow then went invisible over most windows
        cmd += [f"--resize-display={rd}",
                f"--start-late={MATCHBOX} -use_titlebar no -use_cursor yes -kbdconfig {SHARE}/matchbox-kbdconfig"]
    return cmd + QUIET + extra


def build_client_cmd(opts, R, extra):
    cmd = [XPRA, "attach", f"socket://{R}/x.sock", "--backend=terminal"] + CLIENT_QUIET + SHORTCUTS
    if opts["verbose"] >= 2:
        cmd += ["-d", "terminal"]
    return cmd + extra


def exit_status(rc):
    """Popen returncode -> shell-style status."""
    return rc if rc >= 0 else 128 - rc


# ---------- process helpers ----------

def wait_until(pred, secs, step=0.05):
    """Polls pred until it is true or secs have passed; returns its last value."""
    deadline = time.monotonic() + secs
    while not pred():
        if time.monotonic() >= deadline:
            return False
        time.sleep(step)
    return True


def pid_alive(pid):
    """True for a live process; zombies count as gone."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read()
    except OSError:
        return False
    return data[data.rindex(b")") + 2:][:1] != b"Z"


def killpg_quiet(pgid, sig):
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


def kill_quiet(pid, sig):
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass


def kill_pids_with_grace(pids, secs):
    """SIGTERM, then SIGKILL whatever is still alive after secs. Returns the pids that survived both."""
    pids = [p for p in pids if pid_alive(p)]
    for p in pids:
        kill_quiet(p, signal.SIGTERM)
    if not wait_until(lambda: not any(map(pid_alive, pids)), secs):
        for p in pids:
            if pid_alive(p):
                kill_quiet(p, signal.SIGKILL)
        wait_until(lambda: not any(map(pid_alive, pids)), 1)
    return [p for p in pids if pid_alive(p)]


def session_pids(R):
    """Our processes that carry R in their command line or environment, i.e. everything a session started
    (xpra and Xorg name R in their arguments, the WM and COMMAND inherit it through XDG_RUNTIME_DIR/TG_SESSION)."""
    marks = (R.encode() + b"/", R.encode() + b"\0")
    uid, me, out = os.getuid(), os.getpid(), []
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) == me:
            continue
        try:
            if os.stat(f"/proc/{name}").st_uid != uid:
                continue
        except OSError:
            continue
        for part in ("cmdline", "environ"):
            try:
                with open(f"/proc/{name}/{part}", "rb") as f:
                    data = f.read() + b"\0"
            except OSError:
                continue
            if any(m in data for m in marks):
                if pid_alive(int(name)):
                    out.append(int(name))
                break
    return out


def tail(path, n=TAIL_LINES):
    try:
        with open(path, errors="replace") as f:
            lines = collections.deque(f, n)
    except OSError:
        return
    try:
        for line in lines:
            print("  " + line.rstrip("\n"), file=sys.stderr)
    except OSError:
        pass


def newest(pattern):
    paths = glob.glob(pattern)
    return max(paths, key=os.path.getmtime) if paths else None


class Progress:
    """The 'starting COMMAND' line on stderr: shown at once, counts seconds after 5 s, erased before attach."""

    def __init__(self, name, enabled):
        self.name, self.enabled = name, enabled
        self.t0 = time.monotonic()
        self.shown_secs = 0
        self.visible = False
        self.show(f"tg: starting {name} in an X session...")

    def show(self, text):
        if not self.enabled:
            return
        try:
            os.write(2, ("\r\x1b[2K" + text).encode())
            self.visible = bool(text)
        except OSError:
            self.enabled = False

    def tick(self):
        secs = int(time.monotonic() - self.t0)
        if secs >= 5 and secs != self.shown_secs:
            self.shown_secs = secs
            self.show(f"tg: still starting {self.name} in an X session ({secs} s)...")

    def clear(self):
        if self.visible:
            self.show("")


# ---------- runtime directory ----------

def prepare_base(base, uid):
    created = False
    try:
        os.makedirs(base, 0o700)
        created = True
    except FileExistsError:
        pass
    except OSError as e:
        raise Usage(f"cannot create the runtime directory {base}: {e.strerror}.",
                    "set TG_RUNTIME_DIR to a short writable directory.")
    st = os.lstat(base)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != uid:
        raise Usage(f"the runtime directory {base} is not a directory owned by you (or is a symlink).",
                    "remove it or set TG_RUNTIME_DIR to another directory.")
    if created:
        os.chmod(base, 0o700)
    elif stat.S_IMODE(st.st_mode) != 0o700:
        raise Usage(f"the runtime directory {base} has mode {stat.S_IMODE(st.st_mode):o}, expected 700.",
                    f"run chmod 700 {base}, or set TG_RUNTIME_DIR to another directory.")


def make_runtime_dir(env):
    """Creates the private per-session directory R (section 3.2 of the spec) and returns its path."""
    uid, host = os.getuid(), socket.gethostname()
    base = choose_base(env, uid, host)
    if base is None:
        wanted = runtime_bases(env, uid)[0]
        raise Usage(f"the runtime directory {wanted} is too long: xpra's unix sockets under it would exceed "
                    f"{SOCKET_BUDGET} bytes (hostname {host!r}).",
                    "set TG_RUNTIME_DIR to a shorter directory.")
    prepare_base(base, uid)
    while True:
        R = os.path.join(base, base64.b32encode(secrets.token_bytes(5)).decode().lower()[:6])
        try:
            os.mkdir(R, 0o700)
            break
        except FileExistsError:
            continue
    os.chmod(R, 0o700)
    for sub in ("s", "cfg", "xpra", "xorg.conf.d"):
        os.mkdir(os.path.join(R, sub), 0o700)
    os.close(os.open(os.path.join(R, "Xauthority"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    with open(os.path.join(R, "tg.pid"), "w") as f:
        f.write(f"{os.getpid()}\n")
    return R


# ---------- the session ----------

class Session:
    def __init__(self, R, opts, cmd, env):
        self.R, self.opts, self.cmd, self.env = R, opts, cmd, env
        self.sock = f"socket://{R}/x.sock"
        self.started = time.time()
        self.server = self.app = self.client = None
        self.display = self.displayfd = self.app_started = None
        self.x_pids = set()
        self.ready = False
        self.saved_tty = None
        self.client_terminated = self.client_killed = False
        self.done = False
        self.survivors = []
        self.messages = []           # (cause, action, log file to tail); printed after the tty is restored
        self.rwake, self.wwake = os.pipe()
        os.set_blocking(self.wwake, False)
        self.sig = None

    def note(self, msg):
        try:
            with open(os.path.join(self.R, "tg.log"), "a") as f:
                print(f"{time.strftime('%H:%M:%S')}.{int(time.time() * 1000) % 1000:03d} {msg}", file=f)
        except OSError:          # R removed under us (tg --gc from another shell)
            pass

    def fail(self, cause, action, logfile=None):
        self.note(f"failure: {cause}")
        self.messages.append((cause, action, logfile))

    def on_signal(self, signum, frame):
        self.sig = signum
        try:
            os.write(self.wwake, b"x")
        except BlockingIOError:
            pass

    def wait(self, secs):
        """Sleeps up to secs; a signal wakes it early."""
        r, _, _ = select.select([self.rwake], [], [], secs)
        if r:
            os.read(self.rwake, 64)

    # --- startup ---

    def start_server(self, size):
        r, w = os.pipe()
        extra = shlex.split(self.env.get("TG_SERVER_ARGS", ""))
        cmd = build_server_cmd(self.opts, self.R, w, size, self.env.get("TG_XSERVER", "xdummy"), extra)
        self.note("server: " + shlex.join(cmd))
        with open(os.path.join(self.R, "server.log"), "ab") as serverlog:
            self.server = subprocess.Popen(cmd, env=xpra_env(self.env, self.R, True), start_new_session=True,
                                           pass_fds=(w,), stdin=subprocess.DEVNULL, stdout=serverlog,
                                           stderr=serverlog)
        os.close(w)
        self.displayfd = r
        self.note(f"server pid {self.server.pid}")

    def wait_display(self, deadline, tick):
        buf = b""
        try:
            while b"\n" not in buf:
                if self.sig or self.server.poll() is not None or time.monotonic() > deadline:
                    return None
                r, _, _ = select.select([self.displayfd, self.rwake], [], [],
                                        max(0, min(0.5, deadline - time.monotonic())))
                if self.rwake in r:
                    os.read(self.rwake, 64)
                if self.displayfd in r:
                    chunk = os.read(self.displayfd, 64)
                    if not chunk:
                        return None
                    buf += chunk
                tick()
        finally:
            os.close(self.displayfd)
        self.display = ":" + buf.split(b"\n")[0].decode().strip()
        self.note(f"display {self.display}")
        return self.display

    def wait_ready(self, deadline, tick):
        env = xpra_env(self.env, self.R, True)
        while time.monotonic() < deadline and not self.sig and self.server.poll() is None:
            try:
                if subprocess.run([XPRA, "id", self.sock], env=env, timeout=5, check=False,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                    self.ready = True
                    self.x_pids.update(self.read_x_pids())
                    self.note(f"server ready, X pids {sorted(self.x_pids)}")
                    return True
            except subprocess.TimeoutExpired:
                pass
            tick()
            self.wait(0.5)
        return False

    def read_x_pids(self):
        """The X server pid(s) xpra wrote; read early because xpra removes the file when it exits."""
        pids = set()
        for path in glob.glob(os.path.join(self.R, "s", "*", "xvfb.pid")):
            try:
                with open(path) as f:
                    pids.add(int(f.read().strip()))
            except (OSError, ValueError):
                pass
        return pids

    def start_app(self):
        with open(os.path.join(self.R, "app.log"), "ab") as applog:
            self.app = subprocess.Popen(self.cmd, env=app_env(self.env, self.R, self.display),
                                        start_new_session=True, stdin=subprocess.DEVNULL,
                                        stdout=applog, stderr=applog)
        self.app_started = time.monotonic()
        self.note(f"app pid {self.app.pid}: {shlex.join(self.cmd)}")

    def start_client(self):
        self.saved_tty = termios.tcgetattr(0)
        extra = shlex.split(self.env.get("TG_CLIENT_ARGS", ""))
        cmd = build_client_cmd(self.opts, self.R, extra)
        self.client = subprocess.Popen(cmd, env=xpra_env(self.env, self.R, False))
        self.note(f"client pid {self.client.pid}: {shlex.join(cmd)}")

    def write_meta(self):
        meta = {"argv": self.cmd, "mode": "seamless" if self.opts["seamless"] else "desktop",
                "display": self.display, "started": self.started, "cwd": os.getcwd(),
                "pids": {"server": self.server.pid, "app": self.app.pid if self.app else None,
                         "x": sorted(self.x_pids)}}
        with open(os.path.join(self.R, "meta.json"), "w") as f:
            json.dump(meta, f)

    # --- running ---

    def run(self, timeout):
        """Starts everything, supervises, returns tg's exit status. Teardown is the caller's job."""
        progress = Progress(os.path.basename(self.cmd[0]), sys.stderr.isatty())
        try:
            self.start_server(None if self.opts["seamless"] else term_size(0))
            self.write_meta()
            deadline = time.monotonic() + timeout
            if not self.wait_display(deadline, progress.tick) or not self.wait_ready(deadline, progress.tick):
                return self.startup_failed(timeout)
            self.start_app()
            self.write_meta()
        finally:
            progress.clear()
        if self.sig:
            return 128 + self.sig
        if not self.app_group_alive():
            self.note("app exited before the client attached")
            return self.app_finished()
        self.start_client()
        return self.supervise()

    def startup_failed(self, timeout):
        if self.sig:
            return 128 + self.sig
        logs = os.path.join(self.R, "server.log")
        if self.server.poll() is not None:
            self.fail(f"xpra server exited during startup (status {exit_status(self.server.returncode)}).",
                      f"logs kept in {self.R} (server.log, Xorg.log).", logs)
        else:
            where = f" (display {self.display} was allocated)" if self.display else ""
            self.fail(f"xpra server did not become ready within {timeout:g} s{where}.",
                      f"logs kept in {self.R} (server.log, Xorg.log); set TG_TIMEOUT higher if the machine is loaded.",
                      logs)
        return 3

    def supervise(self):
        while True:
            if self.sig:
                self.note(f"signal {self.sig}")
                return 128 + self.sig
            if self.client.poll() is not None or self.server.poll() is not None:
                return self.connection_ended()
            if not self.app_group_alive():
                # A detach or a client crash stops the server and X (--exit-with-client), and COMMAND usually dies
                # of its closed X connection before the client process is gone: that is not COMMAND's own exit.
                t0 = time.monotonic()
                if wait_until(self.x_session_ending, CASCADE_GRACE):
                    self.note(f"app group gone, X session ending after {time.monotonic() - t0:.2f} s")
                    return self.connection_ended()
                code = self.app_finished()
                self.terminate_client()                        # before the server: avoids a staircase on a raw tty
                return code
            self.wait(0.1)

    def x_session_ending(self):
        return (self.client.poll() is not None or self.server.poll() is not None
                or (self.x_pids and not any(map(pid_alive, self.x_pids))))

    def connection_ended(self):
        """The client or the server went away: decides which one ended the session."""
        wait_until(lambda: self.client.poll() is not None, 5)          # the client follows a dying server
        crc, src = self.client.poll(), self.server.poll()
        self.note(f"client {'alive' if crc is None else f'exited {crc}'}, "
                  f"server {'alive' if src is None else f'exited {src}'}")
        if crc is None or src not in (None, 0):
            return self.server_died()
        # the server exits 0 by itself once its client is gone (--exit-with-client)
        return 0 if crc == 0 else self.client_failed()

    def app_finished(self):
        code = exit_status(self.app.returncode)
        ran = time.monotonic() - self.app_started
        self.note(f"app group gone after {ran:.1f} s, status {code}")
        if ran < QUICK_EXIT:
            self.messages.append((f"{self.cmd[0]} exited after {ran:.1f} s with status {code}.",
                                  ("if it opened on your desktop instead, a running instance took over "
                                   "(firefox: try firefox --new-instance -P tg)"), None))
        return code

    def server_died(self):
        rc = self.server.poll()
        cause = "the X server died" if rc is None else f"xpra server exited unexpectedly (status {exit_status(rc)})"
        self.fail(f"{cause}.", f"logs kept in {self.R} (server.log, Xorg.log).", os.path.join(self.R, "server.log"))
        return 3

    def client_failed(self):
        self.fail(f"xpra client failed (status {exit_status(self.client.returncode)}).",
                  f"logs kept in {self.R} (xpra/xpra-terminal-*.log, server.log).",
                  newest(os.path.join(self.R, "xpra", "xpra-terminal-*.log")))
        return 4

    def app_group_alive(self):
        if self.app is None:
            return False
        self.app.poll()
        try:
            os.killpg(self.app.pid, 0)
            return True
        except ProcessLookupError:
            return False

    # --- teardown ---

    def terminate_client(self):
        if self.client is None or self.client.poll() is not None:
            return
        self.client.send_signal(signal.SIGTERM)
        self.client_terminated = True
        if not wait_until(lambda: self.client.poll() is not None, 5):
            self.client.kill()
            self.client.wait()
            self.client_killed = True
        self.note(f"client stopped, status {self.client.returncode}")

    def client_restored_tty(self):
        """Whether the client left the terminal clean: a normal exit, or our SIGTERM (verified clean)."""
        rc = self.client.returncode
        if self.client_killed:
            return False
        return rc == 0 or (self.client_terminated and rc in (128 + signal.SIGTERM, -signal.SIGTERM))

    def restore_tty(self):
        if self.saved_tty is None:
            return
        try:
            termios.tcsetattr(0, termios.TCSADRAIN, self.saved_tty)
            if not self.client_restored_tty():
                os.write(1, RESET_SEQ)
        except (OSError, termios.error):              # the terminal is gone (hangup)
            pass

    def stop_server(self):
        if self.server and self.server.poll() is None:
            # Once a client has attached, its exit already makes the server terminate (--exit-with-client) and
            # `xpra stop` can hang on the closing socket; before readiness the socket may not answer at all.
            if self.ready and self.client is None:
                self.note("teardown: xpra stop")
                try:
                    subprocess.run([XPRA, "stop", self.sock], env=xpra_env(self.env, self.R, True), timeout=3,
                                   check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except subprocess.TimeoutExpired:
                    pass
            if not wait_until(lambda: self.server.poll() is not None, 5 if self.ready else 0):
                self.note("teardown: server still running, SIGTERM to its group")
                killpg_quiet(self.server.pid, signal.SIGTERM)
                if not wait_until(lambda: self.server.poll() is not None, 3):
                    killpg_quiet(self.server.pid, signal.SIGKILL)
                    self.server.wait()
            self.note(f"server stopped, status {self.server.returncode}")
        left = kill_pids_with_grace(self.x_pids | self.read_x_pids(), 3)
        if left:
            self.note(f"teardown: X server {left} survived SIGKILL")

    def stop_app(self):
        if self.app_group_alive():
            self.note("teardown: SIGTERM to the app group")
            killpg_quiet(self.app.pid, signal.SIGTERM)
            if not wait_until(lambda: not self.app_group_alive(), 2):
                killpg_quiet(self.app.pid, signal.SIGKILL)
                wait_until(lambda: not self.app_group_alive(), 1)

    def teardown(self):
        """Client first (tty back within ~1 s), then the server and X, then COMMAND's group. Idempotent."""
        if self.done:
            return
        self.done = True
        self.terminate_client()
        self.restore_tty()
        self.stop_server()
        self.stop_app()
        # anything that escaped the steps above (an X server whose xpra died before writing xvfb.pid, a
        # double-forked child of COMMAND) still carries R in its arguments or environment
        if not wait_until(lambda: not session_pids(self.R), 2):
            self.note(f"teardown: stopping stray processes {session_pids(self.R)}")
            self.survivors = kill_pids_with_grace(session_pids(self.R), 2)
        if self.survivors:
            self.note(f"teardown: survivors {self.survivors}")
            self.messages.append((f"processes of this session are still running: {' '.join(map(str, self.survivors))}.",
                                  "run tg --gc to stop them once they are no longer needed.", None))
        else:
            self.note("teardown: complete")


# ---------- --gc ----------

def session_alive(R):
    try:
        with open(os.path.join(R, "tg.pid")) as f:
            pid = int(f.read().strip())
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return b"tg" in f.read() and pid_alive(pid)
    except (OSError, ValueError):
        return False


def gc(env):
    seen = set()
    for base in runtime_bases(env, os.getuid()):
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for name in names:
            R = os.path.join(base, name)
            if R in seen or not any(os.path.exists(os.path.join(R, f)) for f in ("meta.json", "tg.pid")):
                continue
            seen.add(R)
            if session_alive(R):
                continue
            pids = session_pids(R)
            left = kill_pids_with_grace(pids, 2)
            shutil.rmtree(R, ignore_errors=True)
            n = len(pids) - len(left)
            stopped = f", stopped {n} process{'' if n == 1 else 'es'}" if pids else ""
            print(f"tg: removed {R}{stopped}")
            if left:
                err(f"could not stop {' '.join(map(str, left))}")
    return 0


# ---------- main ----------

def main(argv):
    try:
        opts, cmd = parse_args(argv)
    except Usage as e:
        err(*e.args)
        return 2
    if opts["help"]:
        print(HELP)
        return 0
    if opts["version"]:
        print(f"tg {VERSION}")
        return 0
    env = dict(os.environ)
    if opts["gc"]:
        return gc(env)
    problem = check_terminal(env, os.isatty(0), os.isatty(1))
    if problem:
        err(*problem)
        return 2
    exe = cmd[0]
    if not (os.access(exe, os.X_OK) if "/" in exe else shutil.which(exe)):
        err(f"command not found: {exe}")
        return 2
    try:
        timeout = float(env.get("TG_TIMEOUT", "60"))
    except ValueError:
        err(f"TG_TIMEOUT must be a number of seconds, not {env['TG_TIMEOUT']!r}.")
        return 2
    if env.get("TG_XSERVER", "xdummy") not in ("xdummy", "xvfb"):
        err(f"TG_XSERVER must be xdummy or xvfb, not {env['TG_XSERVER']!r}.")
        return 2
    try:
        R = make_runtime_dir(env)
    except Usage as e:
        err(*e.args)
        return 2
    if env.get("SSH_CONNECTION"):
        err("remote terminal: shared-memory transfer is unavailable, expect higher CPU")

    S = Session(R, opts, cmd, env)
    for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(s, S.on_signal)
    code = 1                                  # an unexpected exception keeps the logs
    try:
        code = S.run(timeout)
    finally:
        S.teardown()
        S.note(f"exit {code}")
        for cause, action, logfile in S.messages:
            err(cause, action)
            if logfile:
                tail(logfile)
        if code != 0 or opts["verbose"] or S.survivors:
            if not any(R in (action or "") for _, action, _ in S.messages):
                err(f"logs: {R}")
        else:
            shutil.rmtree(R, ignore_errors=True)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
