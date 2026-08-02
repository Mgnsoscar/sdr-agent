"""
mDNS service advertisement.

Advertises this agent as an `_sdragent._tcp` service on the local network so the
GUI can auto-discover units by hostname without static IPs. Uses the `zeroconf`
library. If zeroconf isn't installed or registration fails, the agent logs a
warning and continues — discovery is a convenience, not a requirement (the GUI
can still reach units by hostname directly).
"""
from __future__ import annotations

import logging
import socket
from typing import Optional

logger = logging.getLogger(__name__)

SERVICE_TYPE = "_sdragent._tcp.local."


class MdnsAdvertiser:
    def __init__(self, unit_id: str, port: int, agent_version: str,
                 machine_id: str = ""):
        self.unit_id = unit_id
        self.port = port
        self.agent_version = agent_version
        self.machine_id = machine_id
        self._zc = None
        self._info = None

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
        ips = []

        # The primary outbound interface, when there IS a route (put first).
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
        except OSError:
            pass

        # Every non-loopback IPv4 on any interface (works with no default route,
        # e.g. a direct ethernet link).
        try:
            import psutil
            for addrs in psutil.net_if_addrs().values():
                for a in addrs:
                    if (a.family == socket.AF_INET and a.address
                            and not a.address.startswith("127.")
                            and a.address not in ips):
                        ips.append(a.address)
        except Exception:  # noqa: BLE001 — psutil missing or odd platform
            pass

        return ips

    def start(self) -> None:
        try:
            from zeroconf import ServiceInfo, Zeroconf
        except ImportError:
            logger.warning("zeroconf not installed — mDNS advertisement disabled "
                           "(GUI can still connect by hostname)")
            return

        ips = self._local_ips()
        if not ips:
            logger.warning("Could not determine a non-loopback IP — mDNS advertisement "
                           "disabled (add this unit by address in the GUI instead)")
            return

        # Service instance name must be unique on the network.
        instance = f"{self.unit_id}.{SERVICE_TYPE}"
        hostname = socket.gethostname()
        server = hostname if hostname.endswith(".local.") else f"{hostname}.local."

        try:
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
            self._zc = Zeroconf()
            self._zc.register_service(self._info)
            logger.info("mDNS: advertising %s at %s:%d (%s)",
                        self.unit_id, ", ".join(ips), self.port, SERVICE_TYPE)
        except Exception as exc:
            logger.warning("mDNS advertisement failed: %s (continuing without it)", exc)
            self._cleanup()

    def stop(self) -> None:
        self._cleanup()

    def _cleanup(self) -> None:
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