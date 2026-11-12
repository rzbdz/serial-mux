"""smtty / smtty-hermes: interactive and non-interactive serial-mux client."""

import argparse
import json
import os
import select
import socket
import subprocess
import sys
import time
import termios
import tty
from collections import deque
from pathlib import Path

import re
from datetime import datetime

from .config import Config
from .protocol import sync_read_msg, sync_write_msg, b64, unb64
from .state import info_is_running, reconcile_info


class ConnectError(Exception):
    """Raised when the client cannot connect to (or resume) a daemon.

    ``retryable`` is False only for errors that will not resolve by waiting
    (for example, no saved mapping exists for the alias).
    """

    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


def _load_alias_info(config: Config, alias: str) -> dict | None:
    """Load and reconcile an alias record before it is used for recovery."""
    info_path = config.run_dir / f"{alias}.json"
    if not info_path.exists():
        return None
    try:
        return reconcile_info(info_path, json.loads(info_path.read_text()))
    except (json.JSONDecodeError, OSError):
        return None


def resolve_socket(config: Config, alias: str) -> str:
    """Resolve alias to socket path."""
    info = _load_alias_info(config, alias)
    if info:
        return info.get("socket", "")
    # Try as device path
    for f in config.run_dir.glob("*.json"):
        try:
            info = reconcile_info(f, json.loads(f.read_text()))
            if info and info.get("device") == alias:
                return info.get("socket", "")
        except (json.JSONDecodeError, OSError):
            pass
    return ""


def _is_daemon_dead(config: Config, alias: str) -> bool:
    """Check if an alias has saved state but no daemon in this boot."""
    info = _load_alias_info(config, alias)
    return bool(info and not info_is_running(info))


def _auto_resume_daemon(config: Config, alias: str) -> bool:
    """Attempt to restart a dead daemon from its saved metadata. Returns True on success."""
    info = _load_alias_info(config, alias)
    if not info:
        return False

    device = info.get("device")
    baud = info.get("baud", 115200)
    ssh_target = info.get("ssh")
    has_usb_mapping = bool(info.get("usb_port"))

    if not has_usb_mapping and not device and not ssh_target:
        return False

    # Clean up only transient files. Keep the mapping until the replacement
    # daemon has successfully opened its transport.
    (config.run_dir / f"{alias}.pid").unlink(missing_ok=True)
    sock_file = config.sock_dir / f"{alias}.sock"
    sock_file.unlink(missing_ok=True)

    # Build the serial-mux start command
    cmd = [sys.executable, "-m", "serial_mux.cli", "start", "--alias", alias]
    # USB aliases are restored by name so the CLI performs a fresh sysfs
    # reverse lookup. Never feed the last /dev/ttyUSB* name back into start.
    if device and not has_usb_mapping:
        cmd.append(device)
        cmd.extend(["--baud", str(baud)])
    if ssh_target:
        cmd.extend(["--ssh", ssh_target])

    print(f"Resuming dead daemon '{alias}'...", file=sys.stderr, flush=True)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        print(f"Error: Timed out resuming daemon '{alias}'", file=sys.stderr)
        return False

    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        print(f"Error: Failed to resume daemon '{alias}': {stderr}", file=sys.stderr)
        return False

    # Wait for socket to appear
    sock_path = str(config.sock_dir / f"{alias}.sock")
    for _ in range(30):
        if Path(sock_path).exists():
            return True
        time.sleep(0.1)

    print(f"Error: Daemon resumed but socket not ready", file=sys.stderr)
    return False


def connect(
    config: Config,
    alias: str,
    interactive: bool = False,
    token: str | None = None,
) -> tuple[socket.socket, str]:
    """Connect to daemon and perform handshake. Auto-resumes dead daemons.

    ``token`` is presented to the daemon's input gate; without a gate it has
    no effect. Raises :class:`ConnectError` when the daemon cannot be reached;
    its ``retryable`` flag tells interactive mode whether to keep trying.
    """
    sock_path = resolve_socket(config, alias)

    need_resume = False
    if not sock_path:
        # No metadata at all — check if there's a dead daemon to resume
        if _is_daemon_dead(config, alias):
            need_resume = True
        else:
            raise ConnectError(
                f"No daemon found for '{alias}'. "
                f"Start one with: serial-mux start <device> --alias {alias}",
                retryable=False,
            )
    elif not Path(sock_path).exists():
        need_resume = _is_daemon_dead(config, alias)
        if not need_resume:
            raise ConnectError(
                f"Socket {sock_path} not found. Daemon may have crashed."
            )

    if not need_resume and sock_path:
        # Try connecting — may get ConnectionRefused if socket file is stale
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(sock_path)
        except (ConnectionRefusedError, OSError):
            sock.close()
            need_resume = _is_daemon_dead(config, alias)
            if not need_resume:
                raise ConnectError(
                    f"Connection refused to '{alias}'. Daemon may have crashed."
                )

    if need_resume:
        if not _auto_resume_daemon(config, alias):
            raise ConnectError(f"Failed to resume daemon '{alias}'")
        # Re-resolve socket after restart
        sock_path = resolve_socket(config, alias)
        if not sock_path:
            raise ConnectError("Daemon resumed but socket path not found")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(sock_path)

    # Send hello
    hello = {"type": "hello", "interactive": interactive}
    if token:
        hello["token"] = token
    sync_write_msg(sock, hello)

    # Read hello_ack
    msg = sync_read_msg(sock)
    if not msg or msg.get("type") != "hello_ack":
        sock.close()
        raise ConnectError("Unexpected response from daemon")

    # Store transport type for later use
    transport_type = msg.get("transport", "serial")

    return sock, transport_type


_TS_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ")
_SGR_RE = re.compile(r"\x1b\[[0-?]*[ -/]*m")

_TERMINAL_NORMALIZE = (
    b"\x1b[?1049l"  # Leave an alternate screen opened by the remote TUI.
    b"\x0f"  # Select G0; SO may have selected the line-drawing G1 set.
    b"\x1b(B"  # Designate ASCII for G0.
    b"\x1b)B"  # Designate ASCII for G1.
    b"\x1b[0m"  # Reset colors and attributes.
    b"\x1b[?1l"  # Restore normal cursor keys.
    b"\x1b>"  # Restore the numeric keypad.
    b"\x1b[?6l"  # Disable origin mode before resetting the margins.
    b"\x1b[r"  # Restore the full-screen scrolling region.
    b"\x1b[?7h"  # Restore autowrap.
    b"\x1b[?25h"  # Show the cursor.
    # DECOM and DECSTBM home the cursor. Start a fresh line at the bottom
    # afterward so the next output cannot overwrite existing screen text.
    # CUP clamps this row to the screen height without a terminal query.
    b"\x1b[9999;1H\r\n"
)


class _TerminalQueryFilter:
    """Remove terminal queries from an observer's output stream.

    A terminal query sent to every attached client would produce one response
    per terminal.  The remote TUI can then calculate its geometry from the
    wrong response.  Only the active terminal owner receives these queries.
    """

    _WINDOW_REPORTS = {
        11,
        13,
        14,
        15,
        16,
        18,
        19,
        20,
        21,
    }

    def __init__(self):
        self._pending = bytearray()

    @staticmethod
    def _is_query(sequence: bytes) -> bool:
        payload = sequence[2:-1]
        final = sequence[-1:]

        if final in (b"c", b"n", b"x"):
            return True
        if final == b"p" and b"$" in payload:
            return True
        if final != b"t":
            return False

        first_param = payload.split(b";", 1)[0].lstrip(b"?>")
        try:
            operation = int(first_param)
        except ValueError:
            return False
        return operation in _TerminalQueryFilter._WINDOW_REPORTS

    def reset(self):
        self._pending.clear()

    def feed(self, data: bytes) -> bytes:
        """Return data with complete CSI terminal queries removed."""
        source = bytes(self._pending) + data
        self._pending.clear()
        output = bytearray()
        index = 0

        while index < len(source):
            escape = source.find(b"\x1b", index)
            if escape < 0:
                output.extend(source[index:])
                break

            output.extend(source[index:escape])
            if escape + 1 >= len(source):
                self._pending.extend(source[escape:])
                break
            if source[escape + 1] != ord("["):
                output.append(source[escape])
                index = escape + 1
                continue

            final = escape + 2
            while final < len(source):
                if 0x40 <= source[final] <= 0x7E:
                    break
                final += 1
            if final >= len(source):
                self._pending.extend(source[escape:])
                break

            sequence = source[escape:final + 1]
            if not self._is_query(sequence):
                output.extend(sequence)
            index = final + 1

        return bytes(output)


def _strip_timestamp(line: str) -> str:
    """Remove leading [YYYY-MM-DD HH:MM:SS] prefix from a log line."""
    return _TS_RE.sub("", line)


def _sanitize_history_line(line: str) -> str:
    """Keep printable history and SGR while dropping terminal state changes."""
    output = []
    index = 0

    while index < len(line):
        char = line[index]
        code = ord(char)

        if char == "\x1b":
            index += 1
            if index >= len(line):
                break

            introducer = line[index]
            if introducer == "[":
                final = index + 1
                while final < len(line):
                    if 0x40 <= ord(line[final]) <= 0x7E:
                        break
                    final += 1
                if final >= len(line):
                    break
                if line[final] == "m":
                    output.append(line[index - 1:final + 1])
                index = final + 1
                continue

            if introducer in "]PX^_":
                index += 1
                while index < len(line):
                    if line[index] == "\x07":
                        index += 1
                        break
                    if (
                        line[index] == "\x1b"
                        and index + 1 < len(line)
                        and line[index + 1] == "\\"
                    ):
                        index += 2
                        break
                    index += 1
                continue

            while index < len(line):
                code = ord(line[index])
                if not 0x20 <= code <= 0x2F:
                    break
                index += 1
            if index < len(line):
                code = ord(line[index])
                if 0x30 <= code <= 0x7E:
                    index += 1
            continue

        if char == "\r" or code == 0x7F:
            index += 1
            continue
        if code < 0x20 and char != "\t":
            index += 1
            continue

        output.append(char)
        index += 1

    return "".join(output)


def _write_terminal_sequence(fd: int, sequence: bytes):
    """Write a complete control sequence to a terminal when one is present."""
    if not os.isatty(fd):
        return

    remaining = memoryview(sequence)
    try:
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                break
            remaining = remaining[written:]
    except OSError:
        pass


def _normalize_local_terminal(output_fd: int):
    """Put the terminal emulator in a neutral state before rendering."""
    _write_terminal_sequence(output_fd, _TERMINAL_NORMALIZE)


def _restore_local_terminal(
    input_fd: int,
    output_fd: int,
    settings,
):
    """Restore emulator state and the local tty line discipline."""
    try:
        _normalize_local_terminal(output_fd)
    finally:
        termios.tcsetattr(input_fd, termios.TCSADRAIN, settings)


def _rejection_text(msg: dict) -> str:
    """The daemon's refusal plus the note left by whoever turned the gate on."""
    note = msg.get("note")
    return msg.get("message", "input refused") + (f" ({note})" if note else "")


def _write_status(output_fd: int, text: str) -> None:
    """Write a status line framed for raw terminal mode."""
    os.write(output_fd, f"\r\n--- serial-mux: {text} ---\r\n".encode("utf-8", errors="replace"))


def _replay_history(history_msg, timestamps: bool, output_fd: int) -> None:
    """Replay sanitized, de-duplicated scrollback in raw mode."""
    if not history_msg or history_msg.get("type") != "history":
        return
    cleaned = []
    seen_window = deque(maxlen=5)
    for line in history_msg.get("lines", []):
        text = line if timestamps else _strip_timestamp(line)
        text = _sanitize_history_line(text)
        # Compare by plain text (no SGR, no trailing whitespace)
        plain = _SGR_RE.sub("", text).strip()
        if not plain or plain in seen_window:
            continue
        seen_window.append(plain)
        cleaned.append(text)
    for text in cleaned:
        os.write(output_fd, (text + "\r\n").encode("utf-8", errors="replace"))
    # Sanitized history can only change SGR. Reset attributes here, not
    # margins/origin mode: those controls home the cursor and would make
    # the banner and live output overwrite the history just displayed.
    _write_terminal_sequence(output_fd, b"\x1b[0m")


ESCAPE = b"\x01"          # Ctrl-A starts a command, as in picocom
QUIT_KEYS = (b"\x18", b"\x11")   # then Ctrl-X or Ctrl-Q leaves
HELP_KEYS = (b"\x08", b"\x7f", b"?")
ESCAPE_HELP = ("\r\n*** smtty: Ctrl-A Ctrl-X (or Ctrl-Q) leaves, Ctrl-A Ctrl-A sends Ctrl-A, "
               "Ctrl-A Ctrl-H shows this help ***\r\n")


class EscapeKeys:
    """Reads typed keys the way picocom does: Ctrl-A starts a command, and every other key,
    Ctrl-C and Ctrl-] included, goes to the board."""

    def __init__(self) -> None:
        self.armed = False

    def feed(self, data: bytes) -> tuple[bytes, bool, bool]:
        """Returns (bytes to send, leave?, show help?)."""
        out = bytearray()
        quit_, help_ = False, False
        for byte in data:
            ch = bytes([byte])
            if quit_:
                break
            if self.armed:
                self.armed = False
                if ch in QUIT_KEYS:
                    quit_ = True
                elif ch == ESCAPE:
                    out += ESCAPE
                elif ch in HELP_KEYS:
                    help_ = True
                continue
            if ch == ESCAPE:
                self.armed = True
            else:
                out += ch
        return bytes(out), quit_, help_


def _session_loop(sock: socket.socket, timestamps: bool, input_fd: int,
                  output_fd: int, pending_input: bytes = b"") -> str:
    """Run one attached session. Returns ``"detached"`` or ``"lost"``.

    ``pending_input`` (keys typed while reconnecting) is sent first, so the
    Enter that triggered a reconnect still reaches the board.
    """
    terminal_owner = True
    query_filter = _TerminalQueryFilter()
    last_output_was_newline = True
    keys = EscapeKeys()

    if pending_input:
        try:
            sync_write_msg(sock, {"type": "input", "data": b64(pending_input)})
        except (BrokenPipeError, ConnectionResetError, OSError):
            return "lost"

    while True:
        try:
            readable, _, _ = select.select([sys.stdin, sock], [], [], 0.1)
        except (ValueError, OSError):
            return "lost"

        if sys.stdin in readable:
            try:
                typed = os.read(input_fd, 1)
            except OSError:
                return "detached"
            if not typed:                 # EOF
                return "detached"
            ch, leave, show_help = keys.feed(typed)
            if show_help:
                os.write(output_fd, ESCAPE_HELP.encode())
            if leave:
                return "detached"
            if not ch:
                continue

            # If timestamps enabled, handle input newline
            if timestamps and ch in (b"\r", b"\n"):
                ts = datetime.now().strftime("%H:%M:%S")
                # Use \r\n to ensure proper cursor movement in raw mode
                os.write(output_fd, f" [{ts}]\r\n".encode("utf-8"))

            try:
                sync_write_msg(sock, {"type": "input", "data": b64(ch)})
            except (BrokenPipeError, ConnectionResetError, OSError):
                return "lost"

        if sock in readable:
            try:
                # Switch to blocking with timeout for reliable message framing.
                # Non-blocking reads can lose partial header bytes on EAGAIN,
                # corrupting the stream and causing hangs.
                sock.setblocking(True)
                sock.settimeout(2.0)
                msg = sync_read_msg(sock)
                sock.setblocking(False)
                if msg is None:
                    return "lost"

                message_type = msg.get("type")
                if message_type == "terminal_owner":
                    terminal_owner = bool(msg.get("active"))
                    query_filter.reset()
                elif message_type == "input_rejected":
                    _write_status(output_fd, f"{_rejection_text(msg)}; output still shows")
                    last_output_was_newline = True
                elif message_type == "serial_lost":
                    _write_status(
                        output_fd,
                        f"serial lost ({msg.get('reason', 'unknown')}); "
                        "press Enter or wait to reconnect",
                    )
                    last_output_was_newline = True
                elif message_type == "serial_waiting":
                    _write_status(output_fd, msg.get("message", "serial not present yet"))
                    last_output_was_newline = True
                elif message_type == "serial_restored":
                    _write_status(output_fd, f"serial restored: {msg.get('device', 'unknown')}")
                    last_output_was_newline = True
                elif message_type == "transport_changed":
                    _write_status(output_fd, f"transport switched to {msg.get('transport')}")
                    last_output_was_newline = True
                elif message_type == "output":
                    data = unb64(msg["data"])
                    if not terminal_owner:
                        data = query_filter.feed(data)
                    if not data:
                        continue
                    if not timestamps:
                        os.write(output_fd, data)
                    else:
                        # Insert timestamps at the beginning of each line.
                        for b in data:
                            if last_output_was_newline:
                                ts = datetime.now().strftime("[%H:%M:%S] ")
                                os.write(output_fd, ts.encode("utf-8"))
                                last_output_was_newline = False
                            char_bytes = bytes([b])
                            os.write(output_fd, char_bytes)
                            if char_bytes == b"\n":
                                last_output_was_newline = True

            except (TimeoutError, socket.timeout):
                # Timeout reading a complete message — switch back to non-blocking
                sock.setblocking(False)
            except (ConnectionResetError, BrokenPipeError):
                return "lost"
            except Exception:
                sock.setblocking(False)


def _wait_before_retry(input_fd: int, seconds: float, keys: "EscapeKeys | None" = None) -> tuple[bool, bytes]:
    """Wait up to ``seconds`` before the next reconnect attempt.

    Returns ``(keep_going, typed)``. Any key cuts the wait short so pressing
    Enter retries immediately; the keys are returned to be replayed once the
    session is back. Ctrl-A Ctrl-X (or EOF) gives up.
    """
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True, b""
        try:
            readable, _, _ = select.select([sys.stdin], [], [], min(remaining, 0.1))
        except (ValueError, OSError):
            return False, b""
        if sys.stdin in readable:
            try:
                data = os.read(input_fd, 64)
            except OSError:
                return False, b""
            if not data:
                return False, b""
            typed, leave, _ = (keys or EscapeKeys()).feed(data)
            if leave:
                return False, b""
            if not typed:                 # only the start of an escape: keep waiting for its command
                continue
            return True, typed


def _should_reconnect(config: Config, attempt: int) -> bool:
    """Return True if the client should keep trying to reconnect."""
    max_attempts = getattr(config, "client_reconnect_attempts", 0)
    return max_attempts <= 0 or attempt <= max_attempts


def interactive_mode(config: Config, alias: str, timestamps: bool = False,
                     token: str | None = None):
    """Interactive attach mode — like tio/minicom but multiplexed.

    Survives both kinds of loss without the user re-running smtty:
    * serial loss: the daemon stays up, reports ``serial_lost`` and re-binds the
      device on the same USB port (pressing Enter makes it re-probe at once);
    * daemon loss: this loop reconnects (auto-resuming the daemon); pressing
      Enter retries immediately instead of waiting for the next interval.
    """
    input_fd = sys.stdin.fileno()
    output_fd = sys.stdout.fileno()
    old_settings = termios.tcgetattr(input_fd)
    exit_message = None
    attempt = 0
    pending = b""
    waiting_keys = EscapeKeys()      # keys typed while reconnecting, read like the session's

    try:
        tty.setraw(input_fd)
        _normalize_local_terminal(output_fd)

        while True:
            try:
                sock, transport = connect(config, alias, interactive=True, token=token)
                history_msg = sync_read_msg(sock)
            except ConnectError as e:
                if not e.retryable or not _should_reconnect(config, attempt + 1):
                    exit_message = f"cannot connect: {e}"
                    break
                attempt += 1
                if attempt == 1 or pending:
                    _write_status(output_fd, f"{e}; reconnecting (Enter = retry now, Ctrl-A Ctrl-X = quit)")
                keep_going, typed = _wait_before_retry(input_fd, getattr(config, "client_reconnect_interval", 1.0), waiting_keys)
                if not keep_going:
                    exit_message = "detached"
                    break
                pending += typed
                continue
            except (OSError, EOFError) as e:
                attempt += 1
                keep_going, typed = _wait_before_retry(input_fd, getattr(config, "client_reconnect_interval", 1.0), waiting_keys)
                if not keep_going:
                    exit_message = "detached"
                    break
                pending += typed
                continue

            if attempt:
                _write_status(output_fd, "reconnected")
            attempt = 0
            sock.setblocking(False)
            _replay_history(history_msg, timestamps, output_fd)
            print(
                f"\r\n--- serial-mux: attached to {alias} "
                f"[{transport}] (Ctrl-A Ctrl-X to detach) ---\r\n",
                end="",
                flush=True,
            )

            try:
                outcome = _session_loop(sock, timestamps, input_fd, output_fd, pending)
            finally:
                sock.close()
            pending = b""
            if outcome == "detached":
                exit_message = "detached"
                break
            _write_status(output_fd, "daemon connection lost; reconnecting (Enter = retry now)")

    finally:
        _restore_local_terminal(input_fd, output_fd, old_settings)

    if exit_message:
        print(f"--- {exit_message} ---\r\n", end="", flush=True)


def _exit_rejected(sock: socket.socket, msg: dict):
    print(f"Error: {_rejection_text(msg)}", file=sys.stderr)
    sock.close()
    sys.exit(3)


def _last_lines(text: str, n: int) -> str:
    """The last `n` lines of console output, for a timeout's short look back."""
    lines = text.splitlines()
    return "\n".join(lines[-n:]) if lines else ""


def noninteractive_mode(config: Config, alias: str,
                         send_cmd: str, wait_pattern: str = None,
                         timeout: float = 10.0, token: str | None = None,
                         advice: bool = False, tail_lines: int = 40):
    """Non-interactive mode: send a command once, then wait for output."""
    try:
        sock, _transport = connect(config, alias, token=token)
    except ConnectError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Drain history
    msg = sync_read_msg(sock)  # history message

    sock.setblocking(False)
    all_output = ""  # Accumulate ALL output from the moment we send

    # Send command + CR exactly once.  Output handling below is independent of
    # terminal echo, so devices with echo disabled do not trigger a resend.
    cmd_bytes = (send_cmd + "\r").encode("utf-8")
    try:
        sock.setblocking(True)
        sync_write_msg(sock, {"type": "input", "data": b64(cmd_bytes)})
        sock.setblocking(False)
    except (BrokenPipeError, ConnectionResetError):
        print("Error: Connection lost", file=sys.stderr)
        sock.close()
        sys.exit(1)

    # Now wait for pattern if specified and accumulate output.
    if wait_pattern:
        # Compile regex pattern (fall back to literal match on invalid regex)
        try:
            wait_re = re.compile(wait_pattern)
        except re.error:
            wait_re = re.compile(re.escape(wait_pattern))

        deadline = time.time() + timeout
        while time.time() < deadline:
            readable, _, _ = select.select([sock], [], [], 0.1)
            if sock in readable:
                try:
                    msg = sync_read_msg(sock)
                    if msg and msg["type"] == "input_rejected":
                        _exit_rejected(sock, msg)
                    if msg and msg["type"] == "output":
                        all_output += unb64(msg["data"]).decode("utf-8", errors="replace")
                        if wait_re.search(all_output):
                            _print_output(all_output, send_cmd)
                            sock.close()
                            return
                except BlockingIOError:
                    pass
                except Exception:
                    pass

        # Timeout. Scripts get the output and the error as before; `--advice` (for an AI driving it) gives the
        # last few lines and how to go on instead.
        if advice:
            tail = _last_lines(all_output, tail_lines)
            print(f"--- last {tail_lines} line(s) of the console after {timeout:g}s (no match for {wait_pattern!r}) ---", file=sys.stderr)
            print(tail if tail else "(no output)", file=sys.stderr)
            print(f"--- still waiting? run it again with --timeout 30; look further back: smtty {alias} --tail 200", file=sys.stderr)
        else:
            _print_output(all_output, send_cmd)
            print(f"\nError: Timeout waiting for '{wait_pattern}'", file=sys.stderr)
        sock.close()
        sys.exit(2)
    else:
        # No wait pattern — just collect output for a short time
        deadline = time.time() + 1.0
        while time.time() < deadline:
            readable, _, _ = select.select([sock], [], [], 0.1)
            if sock in readable:
                try:
                    msg = sync_read_msg(sock)
                    if msg and msg["type"] == "input_rejected":
                        _exit_rejected(sock, msg)
                    if msg and msg["type"] == "output":
                        all_output += unb64(msg["data"]).decode("utf-8", errors="replace")
                except BlockingIOError:
                    pass
                except Exception:
                    pass

        _print_output(all_output, send_cmd)
        sock.close()


def _print_output(output: str, cmd: str):
    """Print command output, stripping the echo of the command itself."""
    lines = output.split("\n")
    # Find the line with our command and skip it
    found_cmd = False
    for i, line in enumerate(lines):
        if not found_cmd and cmd in line:
            found_cmd = True
            continue
        if found_cmd:
            print(line, end="" if i == len(lines) - 1 else "\n")


def tail_mode(config: Config, alias: str, lines: int = 50):
    """Print the last N lines from the daemon's log files and exit."""
    log_dir = config.logs_dir / alias
    if not log_dir.exists():
        print(f"Error: No logs found for '{alias}'", file=sys.stderr)
        sys.exit(1)

    log_files = sorted(log_dir.glob("*.log"))
    if not log_files:
        print(f"Error: No log files in {log_dir}", file=sys.stderr)
        sys.exit(1)

    # Collect lines from the most recent log files (newest last)
    collected = []
    for log_file in reversed(log_files):
        try:
            with open(log_file, "r", errors="replace") as f:
                file_lines = f.readlines()
            collected = file_lines + collected
            if len(collected) >= lines:
                break
        except OSError:
            continue

    # Take the last N lines
    tail = collected[-lines:]
    for line in tail:
        sys.stdout.write(line if line.endswith("\n") else line + "\n")


def main():
    parser = argparse.ArgumentParser(
        prog="smtty",
        description="serial-mux interactive client"
    )
    parser.add_argument("alias", help="Alias or device path")
    parser.add_argument("--send", "-s", help="Send command (non-interactive mode)")
    parser.add_argument("--wait", "-w", help="Wait for pattern after sending")
    parser.add_argument("--timeout", "-t", type=float, default=10.0,
                        help="Timeout in seconds for --wait (default: 10)")
    parser.add_argument("--timestamps", "-T", action="store_true", default=False,
                        help="Show timestamps on history, input and output lines")
    parser.add_argument("--tail", type=int, nargs="?", const=50, default=None,
                        metavar="N",
                        help="Print last N lines from log and exit (default: 50)")
    parser.add_argument("--advice", action="store_true", default=False,
                        help="With --wait, on timeout: show the last lines and how to go on (for an AI driving it), "
                             "instead of the command's output")
    parser.add_argument("--tail-lines", type=int, default=40, metavar="N",
                        help="With --advice: how many lines a timeout shows (default: 40)")
    parser.add_argument("--token", default=os.environ.get("SERIAL_MUX_TOKEN"),
                        help="Token for a daemon whose input gate is on "
                             "(default: $SERIAL_MUX_TOKEN)")

    args = parser.parse_args()
    config = Config.load()

    if args.tail is not None:
        tail_mode(config, args.alias, lines=args.tail)
    elif args.send:
        noninteractive_mode(config, args.alias,
                           send_cmd=args.send,
                           wait_pattern=args.wait,
                           timeout=args.timeout,
                           token=args.token,
                           advice=args.advice,
                           tail_lines=args.tail_lines)
    else:
        if not sys.stdin.isatty():
            print("Error: Interactive mode requires a terminal", file=sys.stderr)
            sys.exit(1)
        interactive_mode(config, args.alias, timestamps=args.timestamps,
                         token=args.token)


if __name__ == "__main__":
    main()
