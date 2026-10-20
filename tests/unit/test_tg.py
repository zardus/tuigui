import io
import os
import shlex
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))
import tg

R = "/run/user/1000/tg/abcdef"
DESKTOP = {"seamless": False, "verbose": 0}
SEAMLESS = {"seamless": True, "verbose": 0}
USER = {"PATH": "/usr/bin", "HOME": "/home/u", "DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0",
        "XAUTHORITY": "/home/u/.Xauthority", "XPRA_CONF_DIR": "/etc/x", "TERM": "xterm-kitty",
        "KITTY_WINDOW_ID": "7", "LD_LIBRARY_PATH": "/opt/lib", "XDG_RUNTIME_DIR": "/run/user/1000"}


class ParseArgs(unittest.TestCase):
    def test_double_dash_keeps_later_options_for_command(self):
        opts, cmd = tg.parse_args(["-s", "--", "firefox", "-s"])
        self.assertTrue(opts["seamless"])
        self.assertEqual(cmd, ["firefox", "-s"])

    def test_options_after_command_belong_to_command(self):
        opts, cmd = tg.parse_args(["firefox", "--private-window", "-v"])
        self.assertEqual(opts["verbose"], 0)
        self.assertEqual(cmd, ["firefox", "--private-window", "-v"])

    def test_verbose_counts(self):
        self.assertEqual(tg.parse_args(["-vv", "x"])[0]["verbose"], 2)
        self.assertEqual(tg.parse_args(["-v", "--verbose", "x"])[0]["verbose"], 2)

    def test_gc_with_command_is_usage_error(self):
        with self.assertRaises(tg.Usage):
            tg.parse_args(["--gc", "xterm"])

    def test_gc_alone(self):
        opts, cmd = tg.parse_args(["--gc"])
        self.assertTrue(opts["gc"])
        self.assertEqual(cmd, [])

    def test_no_command_is_usage_error(self):
        with self.assertRaises(tg.Usage):
            tg.parse_args([])
        with self.assertRaises(tg.Usage):
            tg.parse_args(["-s", "--"])

    def test_unknown_option(self):
        with self.assertRaises(tg.Usage):
            tg.parse_args(["-x", "xterm"])

    def test_clustered_short_options(self):
        opts, cmd = tg.parse_args(["-sv", "xterm"])
        self.assertEqual((opts["seamless"], opts["verbose"], cmd), (True, 1, ["xterm"]))
        opts, _ = tg.parse_args(["-vs", "xterm"])
        self.assertEqual((opts["seamless"], opts["verbose"]), (True, 1))
        self.assertEqual(tg.parse_args(["-vvv", "x"])[0]["verbose"], 3)

    def test_unknown_letter_in_cluster_names_the_token(self):
        with self.assertRaises(tg.Usage) as cm:
            tg.parse_args(["-sx", "xterm"])
        self.assertIn("'-sx'", cm.exception.args[0])

    def test_help_and_version_need_no_command(self):
        self.assertTrue(tg.parse_args(["--help"])[0]["help"])
        self.assertTrue(tg.parse_args(["-V"])[0]["version"])

    def test_dash_is_a_command(self):
        self.assertEqual(tg.parse_args(["-"])[1], ["-"])


class CheckTerminal(unittest.TestCase):
    def test_no_tty(self):
        cause, _ = tg.check_terminal({"KITTY_WINDOW_ID": "1"}, True, False)
        self.assertIn("must be the kitty terminal", cause)
        self.assertIsNotNone(tg.check_terminal({"KITTY_WINDOW_ID": "1"}, False, True))

    def test_tmux(self):
        cause, action = tg.check_terminal({"TMUX": "/tmp/tmux-1000/default,1,0", "KITTY_WINDOW_ID": "1"},
                                          True, True)
        self.assertIn("inside tmux", cause)
        self.assertIn("plain kitty window", action)

    def test_screen(self):
        cause, _ = tg.check_terminal({"STY": "123.pts-0", "TERM": "xterm-kitty"}, True, True)
        self.assertIn("inside screen", cause)

    def test_zellij(self):
        cause, _ = tg.check_terminal({"ZELLIJ": "0", "ZELLIJ_SESSION_NAME": "x", "TERM": "xterm-kitty",
                                      "KITTY_WINDOW_ID": "1"}, True, True)
        self.assertIn("inside zellij", cause)

    def test_kitty_by_window_id(self):
        self.assertIsNone(tg.check_terminal({"KITTY_WINDOW_ID": "3", "TERM": "xterm-256color"}, True, True))

    def test_kitty_by_term(self):
        self.assertIsNone(tg.check_terminal({"TERM": "xterm-kitty"}, True, True))

    def test_not_kitty(self):
        cause, action = tg.check_terminal({"TERM": "xterm-256color"}, True, True)
        self.assertIn("TERM=xterm-256color", cause)
        self.assertIn("TG_FORCE_TERMINAL=1", action)

    def test_force(self):
        self.assertIsNone(tg.check_terminal({"TERM": "xterm-256color", "TG_FORCE_TERMINAL": "1"}, True, True))

    def test_force_does_not_override_tmux(self):
        self.assertIsNotNone(tg.check_terminal({"TMUX": "x", "TG_FORCE_TERMINAL": "1"}, True, True))


class RuntimeDir(unittest.TestCase):
    def test_bases_order(self):
        env = {"TG_RUNTIME_DIR": "/t", "XDG_RUNTIME_DIR": "/run/user/1000"}
        self.assertEqual(tg.runtime_bases(env, 1000), ["/t", "/run/user/1000/tg", "/tmp/tg-1000"])
        self.assertEqual(tg.runtime_bases({}, 1000), ["/tmp/tg-1000"])

    def test_relative_bases_are_made_absolute(self):
        self.assertEqual(tg.runtime_bases({"TG_RUNTIME_DIR": "rt"}, 1000)[0], os.path.join(os.getcwd(), "rt"))
        self.assertEqual(tg.runtime_bases({"XDG_RUNTIME_DIR": "run"}, 1000)[0], os.path.join(os.getcwd(), "run", "tg"))

    def test_budget(self):
        self.assertTrue(tg.fits_af_unix("/run/user/1000/tg", "laptop"))
        # 17 + 7 + 14 + 63 + 8 = 109 > 100
        self.assertFalse(tg.fits_af_unix("/run/user/1000/tg", "h" * 63))

    def test_long_hostname_falls_back_to_tmp(self):
        env = {"XDG_RUNTIME_DIR": "/run/user/1000"}
        # /run/user/1000/tg: 105 bytes, /tmp/tg-1000: 100 bytes
        self.assertEqual(tg.choose_base(env, 1000, "h" * 59), "/tmp/tg-1000")
        self.assertEqual(tg.choose_base(env, 1000, "laptop"), "/run/user/1000/tg")

    def test_63_char_hostname_fits_nowhere(self):
        self.assertIsNone(tg.choose_base({"XDG_RUNTIME_DIR": "/run/user/1000"}, 1000, "h" * 63))

    def test_long_explicit_dir_is_not_replaced(self):
        env = {"TG_RUNTIME_DIR": "/tmp/" + "x" * 115, "XDG_RUNTIME_DIR": "/run/user/1000"}
        self.assertFalse(tg.fits_af_unix(env["TG_RUNTIME_DIR"], "laptop"))
        self.assertIsNone(tg.choose_base(env, 1000, "laptop"))


class TermSize(unittest.TestCase):
    def size(self, rows, cols, xpx, ypx):
        with mock.patch.object(tg.fcntl, "ioctl", return_value=struct.pack("HHHH", rows, cols, xpx, ypx)):
            return tg.term_size(0)

    def test_exact(self):
        self.assertEqual(self.size(36, 111, 999, 684), (999, 684))

    def test_rounds_down_to_whole_cells(self):
        self.assertEqual(self.size(40, 120, 1205, 805), (1200, 800))

    def test_no_pixels(self):
        self.assertIsNone(self.size(0, 0, 0, 0))
        self.assertIsNone(self.size(40, 120, 0, 0))

    def test_not_a_tty(self):
        with mock.patch.object(tg.fcntl, "ioctl", side_effect=OSError(25, "ENOTTY")):
            self.assertIsNone(tg.term_size(0))


class Environments(unittest.TestCase):
    def test_wayland_gone_everywhere(self):
        for e in (tg.xpra_env(USER, R, True), tg.xpra_env(USER, R, False),
                  tg.app_env(USER, R, ":5")):
            self.assertNotIn("WAYLAND_DISPLAY", e)

    def test_client_keeps_terminal_identity(self):
        e = tg.xpra_env(USER, R, False)
        self.assertEqual(e["TERM"], "xterm-kitty")
        self.assertEqual(e["KITTY_WINDOW_ID"], "7")
        self.assertNotIn("DISPLAY", e)
        self.assertNotIn("XPRA_CONF_DIR", e)

    def test_xauthority_only_for_server_and_app(self):
        self.assertNotIn("XAUTHORITY", tg.xpra_env(USER, R, False))
        self.assertEqual(tg.xpra_env(USER, R, True)["XAUTHORITY"], R + "/Xauthority")
        self.assertEqual(tg.app_env(USER, R, ":5")["XAUTHORITY"], R + "/Xauthority")

    def test_xdg_dirs_only_for_xpra(self):
        e = tg.xpra_env(USER, R, True)
        self.assertEqual(e["XDG_RUNTIME_DIR"], R)
        self.assertEqual(e["XDG_CONFIG_HOME"], R + "/cfg")
        self.assertEqual(e["XPRA_SYSTEM_CONF_DIRS"], tg.XPRA_PREFIX + "/etc/xpra")
        self.assertEqual(e["XPRA_USER_CONF_DIRS"], R + "/cfg/xpra")
        a = tg.app_env(USER, R, ":5")
        self.assertNotIn("XDG_CONFIG_HOME", a)
        self.assertNotIn("XPRA_SYSTEM_CONF_DIRS", a)
        self.assertNotIn("XPRA_USER_CONF_DIRS", a)
        self.assertEqual(a["XDG_RUNTIME_DIR"], "/run/user/1000")

    def test_app_env(self):
        a = tg.app_env(USER, R, ":5")
        self.assertEqual(a["LD_LIBRARY_PATH"], "/opt/lib")
        self.assertEqual(a["PATH"], "/usr/bin")
        self.assertEqual(a["DISPLAY"], ":5")
        self.assertEqual(a["TG_SESSION"], R)
        for k, v in tg.X11_ENV.items():
            self.assertEqual(a[k], v)

    def test_caller_env_untouched(self):
        before = dict(USER)
        tg.xpra_env(USER, R, True)
        tg.app_env(USER, R, ":5")
        self.assertEqual(USER, before)


class ServerCmd(unittest.TestCase):
    def head(self, sub, fd=9):
        return [tg.XPRA, sub, "--daemon=no", f"--displayfd={fd}", f"--bind={R}/x.sock", f"--socket-dirs={R}",
                f"--socket-dir={R}", f"--sessions-dir={R}/s", f"--log-dir={R}", "--exit-with-client=yes",
                "--exit-with-children=no", "--start-new-commands=no", "--server-idle-timeout=300"]

    XORG = (f"--xvfb={tg.XORG} -noreset -novtswitch -nolisten tcp +extension GLX +extension RANDR "
            "+extension RENDER +extension Composite -extension DOUBLE-BUFFER "
            f"-auth {R}/Xauthority -logfile {R}/Xorg.log -configdir {R}/xorg.conf.d "
            f"-config {tg.XPRA_PREFIX}/etc/xpra/xorg.conf")
    XVFB = (f"--xvfb={tg.XVFB} -screen 0 7680x4320x24 +extension GLX +extension RANDR +extension RENDER "
            f"+extension Composite -extension DOUBLE-BUFFER -nolisten tcp -noreset -auth {R}/Xauthority")
    WM = f"--start-late={tg.MATCHBOX} -use_titlebar no -use_cursor yes -kbdconfig {tg.SHARE}/matchbox-kbdconfig"

    def test_desktop_xdummy_with_size(self):
        self.assertEqual(tg.build_server_cmd(DESKTOP, R, 9, (999, 684), "xdummy", []),
                         self.head("start-desktop") + [self.XORG, "--resize-display=yes:999x684", self.WM]
                         + tg.QUIET)

    def test_desktop_xdummy_without_size(self):
        self.assertEqual(tg.build_server_cmd(DESKTOP, R, 9, None, "xdummy", []),
                         self.head("start-desktop") + [self.XORG, "--resize-display=yes", self.WM] + tg.QUIET)

    def test_desktop_xvfb(self):
        self.assertEqual(tg.build_server_cmd(DESKTOP, R, 9, (999, 684), "xvfb", []),
                         self.head("start-desktop") + [self.XVFB, "--resize-display=yes", self.WM] + tg.QUIET)

    def test_seamless(self):
        cmd = tg.build_server_cmd(SEAMLESS, R, 9, None, "xdummy", [])
        self.assertEqual(cmd, self.head("start") + [self.XORG] + tg.QUIET)
        self.assertFalse([a for a in cmd if a.startswith(("--resize-display", "--start=", "--start-late="))])

    def test_extra_args_last(self):
        cmd = tg.build_server_cmd(DESKTOP, R, 9, None, "xdummy", ["--bell=yes", "-d", "x11"])
        self.assertEqual(cmd[-3:], ["--bell=yes", "-d", "x11"])

    def test_paths_with_spaces_survive_xpras_shlex_split(self):
        r = "/tmp/my tg/ab'cd"
        cmd = tg.build_server_cmd(DESKTOP, r, 9, None, "xdummy", [])
        xvfb = shlex.split(next(a for a in cmd if a.startswith("--xvfb="))[len("--xvfb="):])
        self.assertIn(r + "/Xauthority", xvfb)
        self.assertEqual(xvfb[xvfb.index("-logfile") + 1], r + "/Xorg.log")
        self.assertEqual(xvfb[xvfb.index("-configdir") + 1], r + "/xorg.conf.d")
        xvfb = shlex.split(next(a for a in tg.build_server_cmd(DESKTOP, r, 9, None, "xvfb", [])
                                if a.startswith("--xvfb="))[len("--xvfb="):])
        self.assertEqual(xvfb[-1], r + "/Xauthority")

    def test_displayfd_uses_given_fd(self):
        self.assertIn("--displayfd=17", tg.build_server_cmd(DESKTOP, R, 17, None, "xdummy", []))

    def test_quiet_block(self):
        self.assertEqual(" ".join(tg.QUIET),
                         "--mdns=no --dbus-launch=no --dbus=no --dbus-control=no --notifications=no "
                         "--pulseaudio=no --speaker=no --microphone=no --webcam=no --printing=no "
                         "--file-transfer=no --open-files=no --open-url=no --systemd-run=no --html=off "
                         "--http-scripts=off --input-method=none --start-via-proxy=no --source= --tray=no "
                         "--xsettings=no --bell=no")


class ClientCmd(unittest.TestCase):
    def test_snapshot(self):
        self.assertEqual(tg.build_client_cmd(DESKTOP, R, []), [
            tg.XPRA, "attach", f"socket://{R}/x.sock", "--backend=terminal", "--reconnect=no",
            "--speaker=no", "--microphone=no", "--audio=no", "--webcam=no", "--notifications=no", "--printing=no",
            "--file-transfer=no", "--open-files=no", "--open-url=no", "--tray=no", "--mdns=no",
            "--shortcut-modifiers=meta+shift", "--key-shortcut=clear", "--key-shortcut=#+F4:quit"])

    def test_clear_before_quit(self):
        cmd = tg.build_client_cmd(DESKTOP, R, [])
        self.assertLess(cmd.index("--key-shortcut=clear"), cmd.index("--key-shortcut=#+F4:quit"))

    def test_debug_only_at_vv(self):
        self.assertNotIn("-d", tg.build_client_cmd({**DESKTOP, "verbose": 1}, R, []))
        cmd = tg.build_client_cmd({**DESKTOP, "verbose": 2}, R, ["--x=y"])
        self.assertEqual(cmd[-3:], ["-d", "terminal", "--x=y"])


class Misc(unittest.TestCase):
    def test_exit_status(self):
        self.assertEqual(tg.exit_status(3), 3)
        self.assertEqual(tg.exit_status(-9), 137)
        self.assertEqual(tg.exit_status(0), 0)

    def test_reset_seq(self):
        self.assertEqual(tg.RESET_SEQ, b"\x1b[?1049l\x1b[<u\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l"
                                       b"\x1b[?1016l\x1b[?25h\x1b_Ga=d,d=A,q=2\x1b\\\r")

    def test_help_mentions_usage_and_keys(self):
        self.assertTrue(tg.HELP.startswith("tg - run an X11 GUI program inside this kitty terminal\n"))
        self.assertIn("\nusage: tg [-s] [-v] [--] COMMAND [ARGS...]\n", tg.HELP)
        self.assertIn("Alt+Shift+F4", tg.HELP)


class FakeProc:
    def __init__(self, returncode):
        self.returncode = returncode

    def poll(self):
        return self.returncode


class ExitClassification(unittest.TestCase):
    """Session.connection_ended: the client or the server went away; None means still running."""

    def classify(self, client_rc, server_rc, sig=None):
        with tempfile.TemporaryDirectory() as R, mock.patch.object(tg, "wait_until", return_value=False):
            S = tg.Session(R, {"seamless": False, "verbose": 0}, ["app"], {})
            self.addCleanup(os.close, S.rwake)
            self.addCleanup(os.close, S.wwake)
            S.client, S.server, S.sig = FakeProc(client_rc), FakeProc(server_rc), sig
            self.messages = S.messages
            return S.connection_ended()

    def test_group_signal_wins_over_the_dead_client(self):
        self.assertEqual(self.classify(-signal.SIGHUP, None, signal.SIGHUP), 128 + signal.SIGHUP)
        self.assertEqual(self.messages, [])

    def test_detach(self):
        self.assertEqual(self.classify(0, None), 0)
        self.assertEqual(self.classify(0, 0), 0)

    def test_client_failure_takes_the_server_down_cleanly(self):
        self.assertEqual(self.classify(143, None), 4)
        self.assertEqual(self.classify(143, 0), 4)
        self.assertEqual(self.classify(-9, 0), 4)

    def test_server_failure(self):
        self.assertEqual(self.classify(1, 1), 3)
        self.assertEqual(self.classify(0, -9), 3)
        self.assertEqual(self.classify(None, 0), 3)        # the client did not follow within the grace period
        self.assertEqual(self.classify(None, None), 3)     # the X server died under a live xpra server



def session(R):
    S = tg.Session(R, {"seamless": False, "verbose": 0}, ["app"], {})
    os.close(S.rwake)
    os.close(S.wwake)
    return S


class ForeignAppGroup(unittest.TestCase):
    """COMMAND escalated to root (pkexec, sudo): signalling its group fails with EPERM."""

    def test_eperm_means_alive_and_teardown_completes(self):
        with tempfile.TemporaryDirectory() as R, \
                mock.patch.object(tg.os, "killpg", side_effect=PermissionError(1, "EPERM")), \
                mock.patch.object(tg, "wait_until", return_value=False), \
                mock.patch.object(tg, "session_pids", return_value=[]):
            S = session(R)
            S.app = mock.Mock(pid=4242)
            self.assertTrue(S.app_group_alive())
            tg.killpg_quiet(4242, signal.SIGTERM)
            S.teardown()
            self.assertEqual(S.survivors, [4242])
            self.assertIn("still running: 4242", S.messages[0][0])

    def test_kill_quiet_ignores_eperm(self):
        with mock.patch.object(tg.os, "kill", side_effect=PermissionError(1, "EPERM")):
            tg.kill_quiet(1, signal.SIGTERM)

    def test_a_failing_step_does_not_skip_the_rest(self):
        with tempfile.TemporaryDirectory() as R:
            S = session(R)
            calls = []
            S.terminate_client = mock.Mock(side_effect=RuntimeError("boom"), __name__="terminate_client")
            for name in ("restore_tty", "stop_server", "stop_app", "stop_strays"):
                setattr(S, name, mock.Mock(side_effect=lambda n=name: calls.append(n), __name__=name))
            S.teardown()
            self.assertEqual(calls, ["restore_tty", "stop_server", "stop_app", "stop_strays"])


class SessionPids(unittest.TestCase):
    """session_pids matches the variables tg sets and the session's own binaries, not any mention of R."""

    def spawn(self, argv, env, executable=None):
        p = subprocess.Popen(argv, env=env, executable=executable, stdin=subprocess.DEVNULL)
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        return p.pid

    def test_matches(self):
        with tempfile.TemporaryDirectory() as R:
            # the interpreter running the tests is the only program a Nix build sandbox is sure to have
            sleeper = [sys.executable, "-I", "-c", "import time; time.sleep(30)"]
            base = {}
            pager = self.spawn(sleeper + [R + "/tg.log"], {**base, "PWD": R})
            other = self.spawn(sleeper, {**base, "XDG_RUNTIME_DIR": R + "x"})
            ours = [self.spawn(sleeper, {**base, "XDG_RUNTIME_DIR": R}),
                    self.spawn(sleeper, {**base, "TG_SESSION": R}),
                    self.spawn(sleeper, {**base, "XAUTHORITY": R + "/Xauthority"})]
            xorg = self.spawn(["/fake/Xorg-for-Xpra-1", "-c", "import time; time.sleep(30)", "-auth",
                               R + "/Xauthority"], base, executable=sys.executable)
            def renamed():
                with open(f"/proc/{xorg}/cmdline", "rb") as f:
                    return f.read().startswith(b"/fake/Xorg-")
            tg.wait_until(renamed, 5)
            with mock.patch.object(tg, "XORG", "/fake/Xorg"):
                found = set(tg.session_pids(R))
            for p in ours + [xorg]:
                self.assertIn(p, found)
            self.assertNotIn(pager, found)
            self.assertNotIn(other, found)


class RuntimeDirFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp", prefix="tgu")
        self.addCleanup(self.tmp.cleanup)
        self.uid = os.getuid()

    def test_symlink_base_refused(self):
        os.mkdir(self.tmp.name + "/real", 0o700)
        os.symlink(self.tmp.name + "/real", self.tmp.name + "/link")
        with self.assertRaises(tg.Usage):
            tg.prepare_base(self.tmp.name + "/link", self.uid)

    def test_mode_755_base_refused(self):
        os.mkdir(self.tmp.name + "/b")
        os.chmod(self.tmp.name + "/b", 0o755)
        with self.assertRaises(tg.Usage) as cm:
            tg.prepare_base(self.tmp.name + "/b", self.uid)
        self.assertIn("expected 700", cm.exception.args[0])

    def test_fresh_base_is_700(self):
        tg.prepare_base(self.tmp.name + "/new", self.uid)
        self.assertEqual(stat.S_IMODE(os.stat(self.tmp.name + "/new").st_mode), 0o700)

    def test_dollar_in_base_refused(self):
        with self.assertRaises(tg.Usage):
            tg.prepare_base(self.tmp.name + "/$HOME", self.uid)

    def test_make_runtime_dir(self):
        R = tg.make_runtime_dir({"TG_RUNTIME_DIR": self.tmp.name + "/rt"})
        for sub in ("s", "cfg", "xpra", "xorg.conf.d"):
            self.assertTrue(os.path.isdir(os.path.join(R, sub)))
        self.assertEqual(stat.S_IMODE(os.stat(R + "/Xauthority").st_mode), 0o600)
        with open(R + "/tg.pid") as f:
            self.assertEqual(f.read().split()[0], str(os.getpid()))
        self.assertTrue(tg.session_alive(R))

    def test_too_long(self):
        with self.assertRaises(tg.Usage) as cm:
            tg.make_runtime_dir({"TG_RUNTIME_DIR": "/tmp/" + "x" * 115})
        self.assertIn("too long", cm.exception.args[0])

    def write_pid(self, text):
        R = self.tmp.name + "/s"
        os.makedirs(R, exist_ok=True)
        with open(R + "/tg.pid", "w") as f:
            f.write(text)
        return R

    def test_session_alive_checks_the_start_time(self):
        me = os.getpid()
        self.assertTrue(tg.session_alive(self.write_pid(f"{me} {tg.proc_start(me).decode()}\n")))
        self.assertFalse(tg.session_alive(self.write_pid(f"{me} 1\n")))      # pid reused by another process
        self.assertFalse(tg.session_alive(self.write_pid(f"{me}\n")))        # old one-field format
        self.assertFalse(tg.session_alive(self.write_pid("garbage")))
        p = subprocess.Popen(["true"])
        p.wait()
        self.assertFalse(tg.session_alive(self.write_pid(f"{p.pid} 1\n")))
        self.assertTrue(tg.pid_alive(me))
        self.assertFalse(tg.pid_alive(p.pid))

    def gc(self, base):
        out, errs = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", errs):
            tg.gc({"TG_RUNTIME_DIR": base})
        return out.getvalue(), errs.getvalue()

    def test_gc_removes_dead_sessions(self):
        base = self.tmp.name + "/rt"
        os.mkdir(base, 0o700)
        R = base + "/abcdef"
        os.mkdir(R, 0o700)
        with open(R + "/tg.pid", "w") as f:
            f.write(f"{os.getpid()} 1\n")
        out, _ = self.gc(base)
        self.assertEqual(out, f"tg: removed {R}\n")
        self.assertFalse(os.path.exists(R))

    def test_gc_skips_unsafe_bases_and_entries(self):
        real = self.tmp.name + "/real"
        os.mkdir(real, 0o700)
        os.mkdir(real + "/abcdef")
        open(real + "/abcdef/tg.pid", "w").close()
        os.symlink(real, self.tmp.name + "/link")
        out, errs = self.gc(self.tmp.name + "/link")
        self.assertEqual(out, "")
        self.assertIn("skipping", errs)
        os.chmod(real, 0o755)
        out, errs = self.gc(real)
        self.assertEqual(out, "")
        self.assertIn("expected 700", errs)
        os.chmod(real, 0o700)
        os.symlink(real + "/abcdef", real + "/linked")
        os.rename(real + "/abcdef", self.tmp.name + "/elsewhere")
        os.symlink(self.tmp.name + "/elsewhere", real + "/abcdef")
        out, _ = self.gc(real)
        self.assertEqual(out, "")
        self.assertTrue(os.path.exists(self.tmp.name + "/elsewhere/tg.pid"))


class MainValidation(unittest.TestCase):
    def main(self, env):
        errs = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(tg, "check_terminal", return_value=None), \
                mock.patch.object(tg.shutil, "which", return_value="/usr/bin/xterm"), \
                mock.patch.object(tg, "make_runtime_dir", side_effect=AssertionError("session started")), \
                mock.patch.object(sys, "stderr", errs):
            return tg.main(["xterm"]), errs.getvalue()

    def test_bad_timeout_and_xserver(self):
        self.assertEqual(self.main({"TG_TIMEOUT": "abc"})[0], 2)
        self.assertEqual(self.main({"TG_XSERVER": "foo"})[0], 2)

    def test_unbalanced_quotes(self):
        for var in ("TG_SERVER_ARGS", "TG_CLIENT_ARGS"):
            rc, errs = self.main({var: "--shortcut-modifiers='control+shift"})
            self.assertEqual(rc, 2)
            self.assertIn(f"tg: {var}: No closing quotation", errs)


if __name__ == "__main__":
    unittest.main()
