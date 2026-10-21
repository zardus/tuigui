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
import tempfile
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
PROBE_TIMEOUT = 3         # seconds for `xpra id` to answer when listing servers

HELP = """\
tg - run an X11 GUI program inside this kitty terminal

usage: tg [-s] [-v] [--] COMMAND [ARGS...]
       tg [-v] :DISPLAY
       tg
       tg --gc | --help | --version

tg starts a private X server and xpra session, runs COMMAND in it and shows
it in this terminal through the kitty graphics protocol. The program fills
the terminal and follows its size. tg exits when COMMAND exits, or when you
press Alt+Shift+F4 to detach, which also ends COMMAND (in seamless mode only
while COMMAND has a window).

Without arguments, tg lists the running xpra servers you can attach to (in
$XDG_RUNTIME_DIR/xpra, /run/xpra and ~/.xpra). tg's own sessions are not
listed: their client is already attached. tg :DISPLAY attaches to one of them
in this terminal; Alt+Shift+F4 detaches and leaves it running. A client that
is already attached to it is disconnected.

options:
  -s, --seamless   draw each X window where it is instead of one fitted desktop
  -v, --verbose    keep logs on success and print their location (-vv: client debug)
  -V, --version    print versions
  -h, --help       this text
      --gc         clean up sessions whose tg process is gone

keys inside the session:
  Alt+Shift+F4             detach and quit (tg :DISPLAY: detach only)
  Alt+Shift+n / Alt+Shift+p  next / previous window (dialogs, extra windows)
  everything else, including Ctrl+C, goes to the program

environment: TG_XSERVER=xdummy|xvfb  TG_RUNTIME_DIR=DIR  TG_TIMEOUT=SECS
             TG_FORCE_TERMINAL=1  TG_SERVER_ARGS=...  TG_CLIENT_ARGS=...

exit status: COMMAND's own; 2 usage/environment, 3 server failed, 4 client
failed, 128+N when interrupted by signal N.
Logs: $XDG_RUNTIME_DIR/tg/<id>/ (kept with -v or after a failure).
Does not work inside tmux, screen or zellij."""


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
    opts = {"seamless": False, "verbose": 0, "gc": False, "help": False, "version": False, "list": not argv,
            "attach": None}
    argv = list(argv)
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            i += 1
            break
        if not a.startswith("-") or a == "-":
            break
        if len(a) > 2 and a[1] != "-":            # clustered short options: -sv, -vv
            if not all(c in "svhV" for c in a[1:]):
                raise Usage(f"unknown option {a!r}", "see tg --help")
            argv[i:i + 1] = ["-" + c for c in a[1:]]
            continue
        if a in ("-s", "--seamless"):
            opts["seamless"] = True
        elif a in ("-v", "--verbose"):
            opts["verbose"] += 1
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
    if opts["help"] or opts["version"] or opts["list"]:
        return opts, cmd
    if opts["gc"] and cmd:
        raise Usage("--gc takes no COMMAND", "see tg --help")
    if cmd and is_display(cmd[0]):
        if len(cmd) > 1:
            raise Usage(f"tg {cmd[0]} takes no COMMAND: it attaches to the programs already running there",
                        "see tg --help")
        if opts["seamless"]:
            raise Usage("-s does not apply when attaching: the server decides its mode", "see tg --help")
        opts["attach"] = cmd[0]
    if not opts["gc"] and not cmd:
        raise Usage("no COMMAND given", "see tg --help")
    return opts, cmd


def is_display(word):
    """':N', the form of an xpra display to attach to."""
    return word.startswith(":") and word[1:].isdigit()


def check_terminal(env, isatty0, isatty1):
    """Returns None or (cause, action)."""
    if not (isatty0 and isatty1):
        return ("stdin and stdout must be the kitty terminal", None)
    if env.get("TMUX") or env.get("STY") or env.get("ZELLIJ"):
        mux = "tmux" if env.get("TMUX") else "screen" if env.get("STY") else "zellij"
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
        out.append(os.path.abspath(env["TG_RUNTIME_DIR"]))
    if env.get("XDG_RUNTIME_DIR"):
        out.append(os.path.join(os.path.abspath(env["XDG_RUNTIME_DIR"]), "tg"))
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
    # only the pinned build's conf.d: not /etc/xpra, /etc/xdg/xpra, ~/.xpra or ~/.config/xpra
    e["XPRA_SYSTEM_CONF_DIRS"] = XPRA_PREFIX + "/etc/xpra"
    e["XPRA_USER_CONF_DIRS"] = os.path.join(R, "cfg", "xpra")
    if with_xauth:
        e["XAUTHORITY"] = os.path.join(R, "Xauthority")
    return e


def app_env(env, R, display):
    e = {k: v for k, v in env.items() if k not in ("WAYLAND_DISPLAY", "XAUTHORITY")}
    e.update(X11_ENV, DISPLAY=display, XAUTHORITY=os.path.join(R, "Xauthority"), TG_SESSION=R)
    return e


def build_server_cmd(opts, R, w_fd, size, xserver, extra):
    sub = "start" if opts["seamless"] else "start-desktop"
    q = shlex.quote                            # xpra shlex-splits --xvfb and --start-late
    xa = q(os.path.join(R, "Xauthority"))
    if xserver == "xvfb":
        xvfb = (f"{q(XVFB)} -screen 0 7680x4320x24 +extension GLX +extension RANDR +extension RENDER "
                f"+extension Composite -extension DOUBLE-BUFFER -nolisten tcp -noreset -auth {xa}")
    else:
        xvfb = (f"{q(XORG)} -noreset -novtswitch -nolisten tcp +extension GLX +extension RANDR +extension RENDER "
                f"+extension Composite -extension DOUBLE-BUFFER -auth {xa} -logfile {q(R + '/Xorg.log')} "
                f"-configdir {q(R + '/xorg.conf.d')} -config {q(XPRA_PREFIX + '/etc/xpra/xorg.conf')}")
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
                (f"--start-late={q(MATCHBOX)} -use_titlebar no -use_cursor yes "
                 f"-kbdconfig {q(SHARE + '/matchbox-kbdconfig')}")]
    return cmd + QUIET + extra


def build_client_cmd(opts, sock, extra):
    # --reconnect=no: a lost connection means the server is gone (--exit-with-client), so a re-exec'd client
    # would only print another banner on the restored tty
    cmd = ([XPRA, "attach", sock, "--backend=terminal", "--reconnect=no"]
           + CLIENT_QUIET + SHORTCUTS)
    if opts["verbose"] >= 2:
        cmd += ["-d", "terminal"]
    return cmd + extra


def xpra_socket_dirs(env, home):
    """xpra's default socket-dirs, as in its conf.d/10_network.conf."""
    out = []
    if env.get("XDG_RUNTIME_DIR"):
        out.append(os.path.join(env["XDG_RUNTIME_DIR"], "xpra"))
    return out + ["/run/xpra", os.path.join(home, ".xpra")]


def parse_id(text):
    """`xpra id` output (key=value lines) -> dict."""
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def format_servers(servers):
    """Rows of a DISPLAY / TYPE / NAME table, one per server (dicts from parse_id), sorted by display number."""
    def key(s):
        d = s.get("display", "").lstrip(":")
        return (0, int(d), "") if d.isdigit() else (1, 0, d)
    rows = [("DISPLAY", "TYPE", "NAME")] + [(s.get("display", "?"), s.get("session-type", ""),
                                             s.get("session-name", "")) for s in sorted(servers, key=key)]
    w0, w1 = (max(len(r[i]) for r in rows) for i in (0, 1))
    return [f"{a:<{w0}}  {b:<{w1}}  {c}".rstrip() for a, b, c in rows]


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


def proc_start(pid):
    """starttime (field 22 of /proc/PID/stat) of a live, non-zombie process, else None."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read()
    except OSError:
        return None
    fields = data[data.rindex(b")") + 2:].split()   # fields[0] is the state (field 3)
    return None if fields[0] == b"Z" else fields[19]


def killpg_quiet(pgid, sig):
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):  # EPERM: not ours to signal (e.g. COMMAND became root)
        pass


def kill_quiet(pid, sig):
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
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
    """Our processes that a session started: those whose environment has one of the variables tg sets to R
    (xpra, Xorg, the WM: XDG_RUNTIME_DIR; COMMAND and its children: TG_SESSION, XAUTHORITY), plus the session's
    xpra, X server or WM binaries naming R in their arguments. A pager or shell that merely mentions R is not."""
    r = R.encode()
    env_marks = (b"\0XDG_RUNTIME_DIR=" + r + b"\0", b"\0TG_SESSION=" + r + b"\0",
                 b"\0XAUTHORITY=" + r + b"/Xauthority\0")
    exes = tuple(p.encode() for p in (XPRA_PREFIX + "/bin/", XORG, XVFB, MATCHBOX))
    uid, me, out = os.getuid(), os.getpid(), []
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) == me:
            continue
        try:
            if os.stat(f"/proc/{name}").st_uid != uid:
                continue
            with open(f"/proc/{name}/cmdline", "rb") as f:
                argv = f.read().split(b"\0")
        except OSError:
            continue
        try:
            with open(f"/proc/{name}/environ", "rb") as f:
                environ = b"\0" + f.read() + b"\0"
        except OSError:              # the xpra server makes itself non-dumpable: environ is root-only
            environ = b""
        # argv[0]: the X server or WM (a prefix, since xpra renames Xorg's to "<Xorg>-for-Xpra-<display>");
        # argv[1]: xpra's script under its python interpreter
        mine = (any(m in environ for m in env_marks)
                or (any(a.startswith(exes) for a in argv[:2]) and any(r + b"/" in a for a in argv[1:])))
        if mine and pid_alive(int(name)):
            out.append(int(name))
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


def disconnect_reason(log):
    """The reason in the client log's last 'Xpra Session Disconnected: REASON' line, or None."""
    mark = "Xpra Session Disconnected:"
    try:
        with open(log, errors="replace") as f:
            found = [line.split(mark, 1)[1].strip() for line in f if mark in line]
    except (OSError, TypeError):           # TypeError: no log at all (None)
        return None
    return found[-1] if found else None


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

def base_problem(st, uid, check_mode=True):
    """Why a runtime base with lstat result st must not be used, or None."""
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != uid:
        return "is not a directory owned by you (or is a symlink)"
    if check_mode and stat.S_IMODE(st.st_mode) != 0o700:
        return f"has mode {stat.S_IMODE(st.st_mode):o}, expected 700"
    return None


def prepare_base(base, uid):
    if "$" in base:
        raise Usage(f"the runtime directory {base} contains '$', which xpra expands.",
                    "set TG_RUNTIME_DIR to a plain path.")
    created = False
    try:
        os.makedirs(base, 0o700)
        created = True
    except FileExistsError:
        pass
    except OSError as e:
        raise Usage(f"cannot create the runtime directory {base}: {e.strerror}.",
                    "set TG_RUNTIME_DIR to a short writable directory.")
    problem = base_problem(os.lstat(base), uid, check_mode=not created)
    if problem:
        raise Usage(f"the runtime directory {base} {problem}.",
                    f"run chmod 700 {base}, or set TG_RUNTIME_DIR to another directory." if "mode" in problem
                    else "remove it or set TG_RUNTIME_DIR to another directory.")
    if created:
        os.chmod(base, 0o700)


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
    # first, so that --gc can reclaim R if tg is interrupted while setting it up
    with open(os.path.join(R, "tg.pid"), "w") as f:
        f.write(f"{os.getpid()} {proc_start(os.getpid()).decode()}\n")
    os.chmod(R, 0o700)
    for sub in ("s", "cfg", "xpra", "xorg.conf.d"):
        os.mkdir(os.path.join(R, sub), 0o700)
    os.close(os.open(os.path.join(R, "Xauthority"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    return R


# ---------- the session ----------

class Session:
    def __init__(self, R, opts, cmd, env, server_extra=(), client_extra=()):
        self.R, self.opts, self.cmd, self.env = R, opts, cmd, env
        self.server_extra, self.client_extra = list(server_extra), list(client_extra)
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
        self.foreign_survivors = False
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
        cmd = build_server_cmd(self.opts, self.R, w, size, self.env.get("TG_XSERVER", "xdummy"), self.server_extra)
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
        cmd = build_client_cmd(self.opts, self.sock, self.client_extra)
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

    def attach(self, display, path):
        """Attaches to someone else's server; nothing of it is ours to stop. Returns tg's exit status."""
        self.sock = f"socket://{path}"
        self.note(f"attaching to {display} at {path}")
        self.start_client()
        while self.client.poll() is None:
            if self.sig:
                self.note(f"signal {self.sig}")
                return 128 + self.sig
            self.wait(0.1)
        if self.client.returncode != 0:
            return self.client_failed()
        # the client also exits 0 when the server shuts down, and may do so before the server removes its socket
        reason = disconnect_reason(newest(os.path.join(self.R, "xpra", "xpra-terminal-*.log")))
        if reason:
            self.messages.append((f"the xpra server on {display} disconnected: {reason}.", None, None))
        elif socket_listening(path):
            self.messages.append((f"detached; {display} is still running.", f"run tg {display} to attach again.",
                                  None))
        return 0

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
        if self.sig:            # a group-wide signal (window closed, kill -- -pgid) took the client with us
            self.note(f"signal {self.sig}")
            return 128 + self.sig
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
        except PermissionError:       # EPERM: members exist but are not ours to signal (COMMAND became root)
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
            # An attached client's exit usually makes the server terminate by itself (--exit-with-client); a client
            # that never completed its hello does not. `xpra stop` can hang on a closing socket (bounded by the
            # timeout), and before readiness the socket may not answer at all.
            if self.ready and not wait_until(lambda: self.server.poll() is not None, 1 if self.client else 0):
                self.note("teardown: xpra stop")
                try:
                    subprocess.run([XPRA, "stop", self.sock], env=xpra_env(self.env, self.R, True), timeout=3,
                                   check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except subprocess.TimeoutExpired:
                    pass
            if not wait_until(lambda: self.server.poll() is not None, 3 if self.ready else 0):
                self.note("teardown: server still running, SIGTERM to its group")
                killpg_quiet(self.server.pid, signal.SIGTERM)
                if not wait_until(lambda: self.server.poll() is not None, 3):
                    killpg_quiet(self.server.pid, signal.SIGKILL)
                    self.server.wait()
            self.note(f"server stopped, status {self.server.returncode}")
        # only pids that are still this session's: the recorded X server pid may have been reused
        left = kill_pids_with_grace((self.x_pids | self.read_x_pids()) & set(session_pids(self.R)), 3)
        if left:
            self.note(f"teardown: X server {left} survived SIGKILL")

    def stop_app(self):
        if self.app_group_alive():
            self.note("teardown: SIGTERM to the app group")
            killpg_quiet(self.app.pid, signal.SIGTERM)
            if not wait_until(lambda: not self.app_group_alive(), 2):
                killpg_quiet(self.app.pid, signal.SIGKILL)
                if not wait_until(lambda: not self.app_group_alive(), 1):
                    # session_pids() skips processes that are not ours, so it would not report these
                    self.note(f"teardown: app group {self.app.pid} not ours to signal, left running")
                    self.survivors.append(self.app.pid)
                    self.foreign_survivors = True

    def teardown(self):
        """Client first (tty back within ~1 s), then the server and X, then COMMAND's group. Idempotent."""
        if self.done:
            return
        self.done = True
        for step in (self.terminate_client, self.restore_tty, self.stop_server, self.stop_app, self.stop_strays):
            try:
                step()
            except Exception as e:  # noqa: BLE001 -- one failing step must not skip the others
                self.note(f"teardown: {step.__name__} failed: {e!r}")
        if self.survivors:
            self.note(f"teardown: survivors {self.survivors}")
            action = ("they are not yours to signal (COMMAND escalated privileges); stop them as root."
                      if self.foreign_survivors else "run tg --gc to stop them once they are no longer needed.")
            self.messages.append((f"processes of this session are still running: {' '.join(map(str, self.survivors))}.",
                                  action, None))
        else:
            self.note("teardown: complete")

    def stop_strays(self):
        # anything that escaped the steps above (an X server whose xpra died before writing xvfb.pid, a
        # double-forked child of COMMAND) still carries R in its environment
        if not wait_until(lambda: not session_pids(self.R), 2):
            self.note(f"teardown: stopping stray processes {session_pids(self.R)}")
            self.survivors += kill_pids_with_grace(session_pids(self.R), 2)


# ---------- --gc ----------

def session_alive(R):
    """Whether the tg that created R still runs: its pid and start time (immune to pid reuse) match tg.pid."""
    try:
        with open(os.path.join(R, "tg.pid")) as f:
            pid, start = f.read().split()
        return proc_start(int(pid)) == start.encode()
    except (OSError, ValueError):
        return False


def gc(env):
    seen, uid = set(), os.getuid()
    for base in runtime_bases(env, uid):
        try:
            st = os.lstat(base)
        except OSError:
            continue
        problem = base_problem(st, uid)
        if problem:
            err(f"skipping {base}: it {problem}")
            continue
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for name in names:
            R = os.path.join(base, name)
            try:
                st = os.lstat(R)
            except OSError:
                continue
            if R in seen or not stat.S_ISDIR(st.st_mode) or st.st_uid != uid:
                continue
            if not any(os.path.exists(os.path.join(R, f)) for f in ("meta.json", "tg.pid")):
                continue
            seen.add(R)
            if session_alive(R):
                continue
            pids = session_pids(R)
            left = kill_pids_with_grace(pids, 2)
            shutil.rmtree(R, ignore_errors=True)
            n = len(pids) - len(left)
            stopped = f", stopped {n} process{'' if n == 1 else 'es'}" if pids else ""
            if os.path.lexists(R):
                err(f"could not remove {R}")
            else:
                print(f"tg: removed {R}{stopped}")
            if left:
                err(f"could not stop {' '.join(map(str, left))}")
    return 0


# ---------- listing xpra servers ----------

def find_sockets(dirs, uid):
    """Our unix sockets in xpra's socket dirs: DIR/<host>-<display> and DIR/<display>/socket."""
    out = []
    for d in dirs:
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for name in names:
            for path in (os.path.join(d, name), os.path.join(d, name, "socket")):
                try:
                    st = os.lstat(path)
                except OSError:
                    continue
                if stat.S_ISSOCK(st.st_mode) and st.st_uid == uid:
                    out.append(path)
    return out


def socket_listening(path):
    """Whether something accepts connections on a unix socket; `xpra id` on a dead one waits out its timeout."""
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(1)
        try:
            s.connect(path)
            return True
        except OSError:
            return False


def probe_servers(env):
    """[(`xpra id` info, socket path)] of the servers that answer, once each (a server may have several sockets)."""
    sockets = [s for s in find_sockets(xpra_socket_dirs(env, os.path.expanduser("~")), os.getuid())
               if socket_listening(s)]
    servers, seen = [], set()
    if sockets:
        # `xpra id` writes xpra.conf into its user conf dir: give it a throwaway one, as tg does for its sessions
        with tempfile.TemporaryDirectory(prefix="tg-list-") as tmp:
            e = {k: v for k, v in env.items() if k not in XPRA_SCRUB}
            e.update(XDG_CONFIG_HOME=tmp, XPRA_SYSTEM_CONF_DIRS=XPRA_PREFIX + "/etc/xpra",
                     XPRA_USER_CONF_DIRS=os.path.join(tmp, "xpra"))
            procs = [subprocess.Popen([XPRA, "id", f"socket://{s}"], env=e, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                     for s in sockets]
            deadline = time.monotonic() + PROBE_TIMEOUT
            for p, s in zip(procs, sockets):
                try:
                    out = p.communicate(timeout=max(0, deadline - time.monotonic()))[0]
                except subprocess.TimeoutExpired:      # a hung or dead server: not attachable
                    p.kill()
                    p.wait()
                    continue
                info = parse_id(out)
                ident = info.get("uuid") or info.get("display")
                if p.returncode == 0 and ident and ident not in seen:
                    seen.add(ident)
                    servers.append((info, s))
    return servers


def list_servers(env):
    servers = [info for info, _ in probe_servers(env)]
    if not servers:
        err("no running xpra servers found.", "to start a program: tg COMMAND (see tg --help)")
        return 0
    for line in format_servers(servers):
        print(line)
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
    if opts["list"]:
        return list_servers(env)
    problem = check_terminal(env, os.isatty(0), os.isatty(1))
    if problem:
        err(*problem)
        return 2
    display = opts["attach"]
    if display:
        path = next((p for info, p in probe_servers(env) if info.get("display") == display), None)
        if path is None:
            err(f"no xpra server of yours answers on {display}.", "run tg without arguments to list them.")
            return 2
    else:
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
    extra = {}
    for var in ("TG_SERVER_ARGS", "TG_CLIENT_ARGS"):
        try:
            extra[var] = shlex.split(env.get(var, ""))
        except ValueError as e:
            err(f"{var}: {e}.")
            return 2
    try:
        R = make_runtime_dir(env)
    except Usage as e:
        err(*e.args)
        return 2
    if env.get("SSH_CONNECTION"):
        err("remote terminal: shared-memory transfer is unavailable, expect higher CPU")

    S = Session(R, opts, cmd, env, extra["TG_SERVER_ARGS"], extra["TG_CLIENT_ARGS"])
    for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(s, S.on_signal)
    code = 1                                  # an unexpected exception keeps the logs
    try:
        code = S.attach(display, path) if display else S.run(timeout)
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
