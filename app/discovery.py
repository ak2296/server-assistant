"""Make the server easy to find on the home network without knowing its IP address.

* mDNS: the server answers to  http://serverassistant.local:<port>  (Windows 10/11, macOS, iPhone and
  most Linux machines understand this; some Android versions do not, the Android app finds it by itself).
* The Windows computer name works too on most home networks:  http://<computer-name>:<port>
* A small "finder" page (open-server-assistant.html) tries every known address and then scans the
  home network for the server, so a desktop shortcut keeps working even if the router changes the IP.
"""
import logging
import socket

log = logging.getLogger("discovery")
MDNS_NAME = "serverassistant"

_zc = None


def lan_ip() -> str:
    """The address of this computer on the home network."""
    for probe in ("192.168.1.1", "10.0.0.1", "8.8.8.8"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((probe, 80))
            ip = s.getsockname()[0]
            s.close()
            if ip and not ip.startswith("127."):
                return ip
        except OSError:
            continue
    return "127.0.0.1"


def computer_name() -> str:
    return socket.gethostname()


def advertise(port: int) -> bool:
    """Announce  serverassistant.local  on the network. Fails quietly if zeroconf isn't installed."""
    global _zc
    try:
        from zeroconf import ServiceInfo, Zeroconf
    except ImportError:
        log.info("zeroconf not installed; skipping the serverassistant.local name")
        return False
    try:
        ip = lan_ip()
        info = ServiceInfo(
            "_http._tcp.local.", "Server Assistant._http._tcp.local.",
            addresses=[socket.inet_aton(ip)], port=port,
            properties={"app": "server-assistant"}, server=f"{MDNS_NAME}.local.")
        _zc = Zeroconf()
        _zc.register_service(info)
        log.info("Reachable as http://%s.local:%d", MDNS_NAME, port)
        return True
    except Exception as e:
        log.warning("Could not announce %s.local: %s", MDNS_NAME, e)
        return False


def stop() -> None:
    global _zc
    if _zc:
        try:
            _zc.close()
        except Exception:
            pass
        _zc = None
