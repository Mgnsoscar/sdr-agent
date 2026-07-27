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
    def __init__(self, unit_id: str, port: int, agent_version: str):
        self.unit_id = unit_id
        self.port = port
        self.agent_version = agent_version
        self._zc = None
        self._info = None

    def _local_ip(self) -> Optional[str]:
        """Best-effort primary IP of this host (the one used for outbound traffic)."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            # Doesn't actually send anything; just selects the right interface.
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except OSError:
            try:
                return socket.gethostbyname(socket.gethostname())
            except OSError:
                return None

    def start(self) -> None:
        try:
            from zeroconf import ServiceInfo, Zeroconf
        except ImportError:
            logger.warning("zeroconf not installed — mDNS advertisement disabled "
                           "(GUI can still connect by hostname)")
            return

        ip = self._local_ip()
        if ip is None:
            logger.warning("Could not determine local IP — mDNS advertisement disabled")
            return

        # Service instance name must be unique on the network.
        instance = f"{self.unit_id}.{SERVICE_TYPE}"
        hostname = socket.gethostname()
        server = hostname if hostname.endswith(".local.") else f"{hostname}.local."

        try:
            self._info = ServiceInfo(
                type_=SERVICE_TYPE,
                name=instance,
                addresses=[socket.inet_aton(ip)],
                port=self.port,
                properties={
                    "unit_id": self.unit_id,
                    "version": self.agent_version,
                    "api": f"http://{server}:{self.port}",
                },
                server=server,
            )
            self._zc = Zeroconf()
            self._zc.register_service(self._info)
            logger.info("mDNS: advertising %s at %s:%d (%s)",
                        self.unit_id, ip, self.port, SERVICE_TYPE)
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