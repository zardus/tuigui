"""Minimal fake kitty: run a command on a pty with a pixel winsize and answer the queries xpra's terminal client sends.

usage: python3 -I fakekitty.py OUT.bin COMMAND [ARGS...]

Everything the command writes to the tty is recorded in OUT.bin. Graphics queries are answered OK, except shared
memory (t=s), which is refused so frames travel inline and can be decoded from OUT.bin. The kitty keyboard query
(CSI ? u) is answered with flags 31. Exits with the command's status (128+N when killed by signal N).

env: DUR=SECS            SIGTERM the command (SIGKILL its process group 10 s later) when it runs longer than this (15)
     RESIZE=C,R,X,Y@SECS after SECS seconds set the winsize to C columns, R rows, X by Y pixels and send SIGWINCH
     INPUT=BYTES@SECS    after SECS seconds type BYTES (backslash escapes allowed), e.g. kitty's Alt+Shift+F4
                         press and release: '\\x1b[1;4S\\x1b[1;4:3S@10'
"""
import codecs
import fcntl
import os
import pty
import select
import signal
import struct
import sys
import termios
import time

APC_G = b"\x1b_G"
ST = b"\x1b\\"
KB_QUERY = b"\x1b[?u"


def set_winsize(fd, cols, rows, xpx, ypx):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, xpx, ypx))


def parse_resize(spec):
    """'100,30,1000,600@6' -> ((100, 30, 1000, 600), 6.0)"""
    size, at = spec.split("@")
    cols, rows, xpx, ypx = (int(v) for v in size.split(","))
    return (cols, rows, xpx, ypx), float(at)


def parse_input(spec):
    """'\\x1b[1;4S@10' -> (b'\\x1b[1;4S', 10.0)"""
    data, _, at = spec.rpartition("@")
    return codecs.escape_decode(data)[0], float(at)


def graphics_reply(control):
    """The reply kitty would send to a graphics command, or None when it stays silent."""
    keys = dict(kv.split("=", 1) for kv in control.split(",") if "=" in kv)
    if keys.get("q") in ("1", "2") or "i" not in keys:
        return None
    status = "ENOENT:shared memory refused by fakekitty" if keys.get("t") == "s" else "OK"
    return b"\x1b_Gi=%s;%s\x1b\\" % (keys["i"].encode(), status.encode())


def answer(pending):
    """Scan buffered tty output for queries. Returns (replies, unconsumed tail to keep)."""
    replies = []
    while True:
        g, k = pending.find(APC_G), pending.find(KB_QUERY)
        if k >= 0 and (g < 0 or k < g):
            replies.append(b"\x1b[?31u")
            pending = pending[k + len(KB_QUERY):]
            continue
        if g < 0:
            # keep a possible partial escape prefix for the next read
            return replies, pending[-(len(KB_QUERY) - 1):]
        end = pending.find(ST, g)
        if end < 0:
            return replies, pending[g:]
        control = pending[g + len(APC_G):end].split(b";", 1)[0].decode(errors="replace")
        reply = graphics_reply(control)
        if reply:
            replies.append(reply)
        pending = pending[end + len(ST):]


def exit_code(status):
    code = os.waitstatus_to_exitcode(status)
    return code if code >= 0 else 128 - code


def main(argv):
    if len(argv) < 3:
        sys.exit(__doc__)
    out_path, cmd = argv[1], argv[2:]
    duration = float(os.environ.get("DUR", "15"))
    resize = parse_resize(os.environ["RESIZE"]) if os.environ.get("RESIZE") else None
    typed = parse_input(os.environ["INPUT"]) if os.environ.get("INPUT") else None

    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.execvp(cmd[0], cmd)
        finally:
            os._exit(127)
    set_winsize(fd, 120, 40, 1200, 800)

    t0 = time.monotonic()
    pending = b""
    status = None
    stop_sent = None
    eof = False
    with open(out_path, "wb") as out:
        while status is None:
            now = time.monotonic() - t0
            if resize and now >= resize[1]:
                set_winsize(fd, *resize[0])
                try:
                    os.killpg(os.tcgetpgrp(fd), signal.SIGWINCH)
                except OSError:
                    os.killpg(pid, signal.SIGWINCH)
                print(f"fakekitty: t={now:.1f}s resized to {resize[0]}", file=sys.stderr)
                resize = None
            if typed and now >= typed[1]:
                os.write(fd, typed[0])
                print(f"fakekitty: t={now:.1f}s typed {typed[0]!r}", file=sys.stderr)
                typed = None
            if stop_sent is None and now >= duration:
                print(f"fakekitty: t={now:.1f}s DUR reached, sending SIGTERM", file=sys.stderr)
                os.kill(pid, signal.SIGTERM)
                stop_sent = now
            elif stop_sent is not None and now >= stop_sent + 10:
                os.killpg(pid, signal.SIGKILL)

            if eof:
                time.sleep(0.1)
            elif select.select([fd], [], [], 0.2)[0]:
                try:
                    data = os.read(fd, 65536)
                except OSError:  # EIO: every slave fd is closed
                    data = b""
                eof = not data
                out.write(data)
                out.flush()
                replies, pending = answer(pending + data)
                for reply in replies:
                    os.write(fd, reply)
            done, st = os.waitpid(pid, os.WNOHANG)
            if done:
                status = st
        # what the command wrote just before exiting is still buffered in the pty
        while not eof and select.select([fd], [], [], 0.2)[0]:
            try:
                data = os.read(fd, 65536)
            except OSError:
                break
            eof = not data
            out.write(data)
    os.close(fd)
    sys.exit(exit_code(status))


if __name__ == "__main__":
    main(sys.argv)
