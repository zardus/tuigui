import os
import struct
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
        a = tg.app_env(USER, R, ":5")
        self.assertNotIn("XDG_CONFIG_HOME", a)
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
            tg.XPRA, "attach", f"socket://{R}/x.sock", "--backend=terminal",
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

    def classify(self, client_rc, server_rc):
        with tempfile.TemporaryDirectory() as R, mock.patch.object(tg, "wait_until", return_value=False):
            S = tg.Session(R, {"seamless": False, "verbose": 0}, ["app"], {})
            self.addCleanup(os.close, S.rwake)
            self.addCleanup(os.close, S.wwake)
            S.client, S.server = FakeProc(client_rc), FakeProc(server_rc)
            return S.connection_ended()

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


if __name__ == "__main__":
    unittest.main()
