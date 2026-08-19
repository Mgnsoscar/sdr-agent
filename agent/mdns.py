"""
mDNS service advertisement.

Advertises this agent as an `_sdragent._tcp` service on the local network so the
GUI can auto-discover units by hostname without static IPs. Uses the `zeroconf`
library. If zeroconf isn't installed or registration fails, the agent logs a
warning and continues — discovery is a convenience, not a requirement (the GUI
can still reach units by hostname directly).

Advertisement is refreshed on a background thread: it retries until a usable
(non-loopback) IP exists, and re-binds/re-registers whenever the host's addresses
change. This matters on a direct-ethernet link, where the link-local (169.254.x)
address only appears once the cable is up — often AFTER the agent booted — and
would otherwise be missed forever.
"""
from __future__ import annotations

import logging
import os
import socket
import threading
from typing import List, Optional

logger = logging.getLogger(__name__)

SERVICE_TYPE = "_sdragent._tcp.local."

# Interfaces whose addresses must NEVER be advertised — internal/management NICs a
# client can't (and shouldn't) reach. Comma-separated names in SDR_MDNS_EXCLUDE_IFACES.
# Empty by default (the Pi units are unaffected); the X410 sets this to "int0" so its
# internal RFSoC management address (169.254.0.1) is never announced as a unit address.
def _excluded_ifaces() -> set:
    raw = os.environ.get("SDR_MDNS_EXCLUDE_IFACES", "")
    return {n.strip() for n in raw.split(",") if n.strip()}

# How often to re-check the host's addresses (seconds). A change triggers a
# re-advertise; steady state is just a cheap interface enumeration.
REFRESH_INTERVAL_S = 15.0


class MdnsAdvertiser:
    def __init__(self, unit_id: str, port: int, agent_version: str,
                 machine_id: str = ""):
        self.unit_id = unit_id
        self.port = port
        self.agent_version = agent_version
        self.machine_id = machine_id
        self._zc = None
        self._info = None
        self._current_ips: List[str] = []
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _local_ip(self) -> Optional[str]:
        """Kept for compatibility — the first real (non-loopback) address, or None."""
        ips = self._local_ips()
        return ips[0] if ips else None

    def _local_ips(self):
        """Every real IPv4 address of this host — the addresses a client can
        actually reach us at. LOOPBACK IS EXCLUDED: on Debian/Raspberry Pi OS the
        machine's own hostname is mapped to 127.0.1.1 in /etc/hosts, so resolving
        our hostname (or a fragile default-route probe with no internet) yields a
        loopback the client can't use. We enumerate interfaces instead (psutil), so
        the wifi and/or ethernet IP is advertised regardless of internet access."""
        excluded_ifaces = _excluded_ifaces()

        # Addresses that belong to an excluded interface — filtered from EVERY path
        # (even the default-route probe), so an internal NIC's address can't slip in.
        excluded_addrs = set()
        try:
            import psutil
            for name, addrs in psutil.net_if_addrs().items():
                if name in excluded_ifaces:
                    for a in addrs:
                        if a.family == socket.AF_INET and a.address:
                            excluded_addrs.add(a.address)
        except Exception:  # noqa: BLE001 — psutil missing or odd platform
            pass

        ips = []

        # The primary outbound interface, when there IS a route (put first).
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            if not ip.startswith("127.") and ip not in ips and ip not in excluded_addrs:
                ips.append(ip)
        except OSError:
            pass

        # Every non-loopback IPv4 on any interface (works with no default route,
        # e.g. a direct ethernet link), skipping excluded/internal interfaces.
        try:
            import psutil
            for name, addrs in psutil.net_if_addrs().items():
                if name in excluded_ifaces:
                    continue
                for a in addrs:
                    if (a.family == socket.AF_INET and a.address
                            and not a.address.startswith("127.")
                            and a.address not in ips
                            and a.address not in excluded_addrs):
                        ips.append(a.address)
        except Exception:  # noqa: BLE001 — psutil missing or odd platform
            pass

        return ips

    def start(self) -> None:
        try:
            import zeroconf  # noqa: F401 — probe availability once
        except ImportError:
            logger.warning("zeroconf not installed — mDNS advertisement disabled "
                           "(GUI can still connect by hostname)")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="mdns-advertise",
                                        daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        # Advertise now if we can, then keep the advertisement in sync with the
        # host's addresses (retrying while there is none — e.g. before a direct
        # ethernet link's link-local address is assigned).
        while True:
            try:
                self._sync()
            except Exception as exc:  # noqa: BLE001 — never let the loop die
                logger.debug("mDNS refresh error: %s", exc)
            if self._stop_event.wait(REFRESH_INTERVAL_S):
                return

    def _sync(self) -> None:
        """Re-advertise iff the set of addresses changed. Recreates the Zeroconf
        instance on a change so it (re-)binds interfaces that came up after start."""
        ips = sorted(self._local_ips())
        if ips == self._current_ips:
            return
        self._teardown()
        self._current_ips = ips
        if not ips:
            logger.info("mDNS: no non-loopback IP yet — will retry (add by address "
                        "in the GUI meanwhile)")
            return
        try:
            from zeroconf import ServiceInfo, Zeroconf, InterfaceChoice
            instance = f"{self.unit_id}.{SERVICE_TYPE}"
            hostname = socket.gethostname()
            server = hostname if hostname.endswith(".local.") else f"{hostname}.local."
            self._info = ServiceInfo(
                type_=SERVICE_TYPE,
                name=instance,
                addresses=[socket.inet_aton(ip) for ip in ips],
                port=self.port,
                properties={
                    "unit_id": self.unit_id,
                    "machine_id": self.machine_id,
                    "version": self.agent_version,
                    "api": f"http://{server}:{self.port}",
                },
                server=server,
            )
            # InterfaceChoice.All so a link-local-only interface is bound too.
            self._zc = Zeroconf(interfaces=InterfaceChoice.All)
            self._zc.register_service(self._info)
            logger.info("mDNS: advertising %s at %s:%d (%s)",
                        self.unit_id, ", ".join(ips), self.port, SERVICE_TYPE)
        except Exception as exc:  # noqa: BLE001
            logger.warning("mDNS advertisement failed: %s (will retry)", exc)
            self._teardown()
            self._current_ips = []   # force a fresh attempt next cycle

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        self._teardown()
        self._current_ips = []

    def _teardown(self) -> None:
        try:
            if self._zc and self._info:
                self._zc.unregister_service(self._info)
            if self._zc:
                self._zc.close()
        except Exception:
            pass
        finally:
            self._zc = None
            self._info = None