"""End-to-end: a USB serial alias follows its physical port across a link drop.

Acceptance contract (sw1 requirement): after the serial link drops, once a TTY
enumerates on the *same USB port* again, pressing Enter in smtty re-binds it and
the Enter reaches the device. The USB topology is simulated with a port map file
(port -> current TTY path) patched over the sysfs lookups; socat provides the
TTYs, killed and recreated to model unplug / re-enumeration.
"""

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from serial_mux.protocol import b64, sync_read_msg, sync_write_msg, unb64
from tests.conftest import PYTHON, connect_to_daemon

PORT = "pci0000:00/usb/usb-2/usb-2:1.0"


def _socat(link_a, link_b):
    proc = subprocess.Popen(
        ["socat", f"pty,raw,echo=0,link={link_a}", f"pty,raw,echo=0,link={link_b}"],
        stderr=subprocess.DEVNULL,
    )
    for _ in range(50):
        if os.path.exists(link_a) and os.path.exists(link_b):
            return proc
        time.sleep(0.1)
    proc.kill()
    raise RuntimeError("socat did not create PTYs")


def _set_port(port_map: Path, tty: str | None, instance: str = "1:4"):
    port_map.write_text(json.dumps({PORT: {"tty": tty, "instance": instance}} if tty else {}))


def _start_daemon(tmp_config, alias, device, port_map, interval):
    script = f"""
import asyncio, json, logging, sys
from pathlib import Path
sys.path.insert(0, '.')
from serial_mux import daemon as d
from serial_mux.config import Config

PORT_MAP = Path({str(port_map)!r})

def _load():
    try:
        return json.loads(PORT_MAP.read_text())
    except Exception:
        return {{}}

def find_usb_device(port):
    e = _load().get(port)
    if not e or not e.get("tty") or not Path(e["tty"]).exists():
        return None, {{}}
    return e["tty"], {{"usb_port": port, "usb_instance": e["instance"]}}

def inspect_usb_device(device):
    for port, e in _load().items():
        if e.get("tty") == device and Path(device).exists():
            return {{"usb_port": port, "usb_instance": e["instance"]}}
    return {{}}

d.find_usb_device = find_usb_device
d.inspect_usb_device = inspect_usb_device
logging.basicConfig(level=logging.DEBUG)
cfg = Config()
cfg.base_dir = Path({str(tmp_config.base_dir)!r})
cfg.config_dir = Path({str(tmp_config.config_dir)!r})
cfg.serial_reconnect_interval = {interval}
cfg.ensure_dirs()
asyncio.run(d.SerialDaemon({device!r}, 115200, {alias!r}, cfg).run())
"""
    proc = subprocess.Popen(
        [PYTHON, "-c", script], cwd=str(Path(__file__).parent.parent),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    sock_path = tmp_config.sock_dir / f"{alias}.sock"
    for _ in range(50):
        if sock_path.exists():
            return proc
        time.sleep(0.1)
    proc.kill()
    raise RuntimeError("daemon socket not created")


def _wait_for(sock, mtype, timeout=5.0):
    deadline = time.monotonic() + timeout
    seen = []
    while time.monotonic() < deadline:
        try:
            msg = sync_read_msg(sock)
        except (TimeoutError, OSError):
            continue
        if msg is None:
            break
        seen.append(msg.get("type"))
        if msg.get("type") == mtype:
            return msg
    raise AssertionError(f"no {mtype!r} within {timeout}s (saw {seen})")


def _read_peer(peer_path, want: bytes, timeout=5.0) -> bytes:
    fd = os.open(peer_path, os.O_RDONLY | os.O_NONBLOCK)
    got = b""
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and want not in got:
            try:
                got += os.read(fd, 1024)
            except BlockingIOError:
                time.sleep(0.05)
    finally:
        os.close(fd)
    return got


def _stop(proc):
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.mark.timeout(60)
def test_enter_rebinds_same_usb_port_after_link_drop(tmp_config, tmp_path):
    a1, b1 = str(tmp_path / "ttyUSB0"), str(tmp_path / "peer0")
    a2, b2 = str(tmp_path / "ttyUSB1"), str(tmp_path / "peer1")
    port_map = tmp_path / "ports.json"
    alias = "board1"

    socat1 = _socat(a1, b1)
    _set_port(port_map, a1)
    # interval 0: no background probing, so only typed input can trigger re-bind
    daemon = _start_daemon(tmp_config, alias, a1, port_map, interval=0)
    socat2 = None
    try:
        sock, _, _ = connect_to_daemon(tmp_config, alias, interactive=True)

        with open(b1, "wb", buffering=0) as peer:
            peer.write(b"before\r\n")
        assert b"before" in unb64(_wait_for(sock, "output")["data"])

        # link drop: the USB-UART disappears
        _set_port(port_map, None)
        socat1.kill()
        socat1.wait()
        lost = _wait_for(sock, "serial_lost")
        assert lost["waiting"] is True and lost["usb_port"] == PORT
        info = json.loads((tmp_config.run_dir / f"{alias}.json").read_text())
        assert info["usb_port"] == PORT and info["serial_state"] == "waiting"

        # Enter while the port is still empty: told once, nothing bound
        sync_write_msg(sock, {"type": "input", "data": b64(b"\r")})
        waiting = _wait_for(sock, "serial_waiting")
        assert waiting["usb_port"] == PORT

        # the device re-enumerates on the SAME port under a NEW tty name
        socat2 = _socat(a2, b2)
        _set_port(port_map, a2, instance="1:9")
        time.sleep(0.5)  # background probing is off: nothing may happen yet
        info = json.loads((tmp_config.run_dir / f"{alias}.json").read_text())
        assert info["serial_state"] == "waiting"

        # Enter -> immediate probe -> re-bind, and the Enter reaches the device
        sync_write_msg(sock, {"type": "input", "data": b64(b"\r")})
        restored = _wait_for(sock, "serial_restored")
        assert restored["device"] == a2 and restored["usb_port"] == PORT
        assert b"\r" in _read_peer(b2, b"\r")

        with open(b2, "wb", buffering=0) as peer:
            peer.write(b"after\r\n")
        assert b"after" in unb64(_wait_for(sock, "output")["data"])
        info = json.loads((tmp_config.run_dir / f"{alias}.json").read_text())
        assert info["device"] == a2 and info["usb_instance"] == "1:9"
        sock.close()
    finally:
        _stop(daemon)
        for p in (socat1, socat2):
            if p and p.poll() is None:
                p.kill()


@pytest.mark.timeout(60)
def test_background_probe_rebinds_without_input(tmp_config, tmp_path):
    a1, b1 = str(tmp_path / "ttyUSB0"), str(tmp_path / "peer0")
    port_map = tmp_path / "ports.json"
    alias = "board2"
    socat1 = _socat(a1, b1)
    _set_port(port_map, a1)
    daemon = _start_daemon(tmp_config, alias, a1, port_map, interval=0.2)
    socat2 = None
    try:
        sock, _, _ = connect_to_daemon(tmp_config, alias)
        _set_port(port_map, None)
        socat1.kill()
        socat1.wait()
        _wait_for(sock, "serial_lost")
        socat2 = _socat(a1, b1)
        _set_port(port_map, a1, instance="1:5")
        assert _wait_for(sock, "serial_restored")["device"] == a1
        sock.close()
    finally:
        _stop(daemon)
        for p in (socat1, socat2):
            if p and p.poll() is None:
                p.kill()


@pytest.mark.timeout(60)
def test_device_on_another_port_is_never_bound(tmp_config, tmp_path):
    a1, b1 = str(tmp_path / "ttyUSB0"), str(tmp_path / "peer0")
    a2, b2 = str(tmp_path / "ttyUSB1"), str(tmp_path / "peer1")
    port_map = tmp_path / "ports.json"
    alias = "board3"
    socat1 = _socat(a1, b1)
    _set_port(port_map, a1)
    daemon = _start_daemon(tmp_config, alias, a1, port_map, interval=0.1)
    socat2 = None
    try:
        sock, _, _ = connect_to_daemon(tmp_config, alias)
        _set_port(port_map, None)
        socat1.kill()
        socat1.wait()
        _wait_for(sock, "serial_lost")
        # a TTY appears, but on a different physical port
        socat2 = _socat(a2, b2)
        port_map.write_text(json.dumps({"pci0000:00/usb/usb-3/usb-3:1.0": {"tty": a2, "instance": "2:2"}}))
        sync_write_msg(sock, {"type": "input", "data": b64(b"\r")})
        _wait_for(sock, "serial_waiting")
        time.sleep(0.5)
        info = json.loads((tmp_config.run_dir / f"{alias}.json").read_text())
        assert info["serial_state"] == "waiting" and info["device"] is None
        sock.close()
    finally:
        _stop(daemon)
        for p in (socat1, socat2):
            if p and p.poll() is None:
                p.kill()


@pytest.mark.timeout(60)
def test_saved_alias_starts_while_port_is_empty(tmp_config, tmp_path):
    """Daemon resumed after the port vanished (e.g. host reboot) waits for it."""
    port_map = tmp_path / "ports.json"
    _set_port(port_map, None)
    script = f"""
import asyncio, sys
from pathlib import Path
sys.path.insert(0, '.')
from serial_mux import daemon as d
from serial_mux.config import Config
d.find_usb_device = lambda port: (None, {{}})
cfg = Config()
cfg.base_dir = Path({str(tmp_config.base_dir)!r})
cfg.config_dir = Path({str(tmp_config.config_dir)!r})
cfg.ensure_dirs()
asyncio.run(d.SerialDaemon(None, 115200, "board4", cfg, usb_port={PORT!r}).run())
"""
    proc = subprocess.Popen([PYTHON, "-c", script], cwd=str(Path(__file__).parent.parent),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        info_path = tmp_config.run_dir / "board4.json"
        for _ in range(50):
            if info_path.exists():
                break
            time.sleep(0.1)
        info = json.loads(info_path.read_text())
        assert info["usb_port"] == PORT and info["serial_state"] == "waiting"
    finally:
        _stop(proc)
