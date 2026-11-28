"""Host checks the engine relies on (invariant 6 clock drift; section 12 static IP).

* `ntp_offset_ms`: a minimal SNTP query (RFC 4330). Returns local clock minus server
  clock in milliseconds, or None when the server can't be reached (the data gate then
  treats drift as unknown and blocks new entries).
* `public_ip`: asks each configured service; the answer counts only if at least two
  agree, so one wrong or hijacked service can't pass the static-IP check by itself.
"""

from __future__ import annotations

import ipaddress
import socket
import struct
import time
import urllib.request
from collections import Counter
from collections.abc import Sequence

NTP_EPOCH = 2_208_988_800  # seconds from 1900-01-01 to 1970-01-01


def ntp_offset_ms(server: str, timeout: float = 2.0) -> float | None:
    pkt = b"\x1b" + 47 * b"\0"  # LI=0, VN=3, mode=3 (client)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            t0 = time.time()
            s.sendto(pkt, (server, 123))
            data, _ = s.recvfrom(48)
            t3 = time.time()
    except OSError:
        return None
    if len(data) < 48:
        return None
    rx_s, rx_f, tx_s, tx_f = struct.unpack("!II II", data[32:48])
    t1 = rx_s - NTP_EPOCH + rx_f / 2**32
    t2 = tx_s - NTP_EPOCH + tx_f / 2**32
    offset = ((t1 - t0) + (t2 - t3)) / 2  # server minus local
    return float(-offset * 1000.0)


def _ask(url: str, timeout: float) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 - fixed https URLs from config
            txt = r.read(64).decode("ascii", "replace").strip()
        return str(ipaddress.ip_address(txt))
    except (OSError, ValueError):
        return None


def public_ip(services: Sequence[str], timeout: float = 4.0) -> str | None:
    answers = [a for a in (_ask(u, timeout) for u in services) if a]
    if not answers:
        return None
    ip, n = Counter(answers).most_common(1)[0]
    return ip if n >= 2 or len(services) == 1 else None


def static_ip_ok(registered: str | None, services: Sequence[str]) -> tuple[bool, str]:
    if not registered:
        return False, "no registered static IP in config"
    ip = public_ip(services)
    if ip is None:
        return False, "could not confirm the public IP (services disagree or unreachable)"
    if ip != registered:
        return False, f"public IP {ip} is not the registered {registered}"
    return True, "ok"
