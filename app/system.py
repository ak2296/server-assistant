"""PC health: CPU, RAM, drives, network, uptime, Plex processes, NVIDIA GPU."""
import shutil
import subprocess
import threading
import time
from collections import deque

import psutil

HISTORY = 60  # samples kept for the mini graphs (one every 5 s = 5 minutes)
PLEX_PROCS = ("plex media server", "plex transcoder", "plexmediaserver", "plex tuner service")


class SystemMonitor:
    def __init__(self, drives: list[str] | None = None):
        self.drives = [d.rstrip("\\/").upper() for d in (drives or [])]
        self.history = {k: deque(maxlen=HISTORY) for k in ("cpu", "ram", "net_down", "net_up", "disk_read", "disk_write")}
        self.lock = threading.Lock()
        self._net = psutil.net_io_counters()
        self._disk = psutil.disk_io_counters()
        self._t = time.time()
        self._gpu_cmd = shutil.which("nvidia-smi")
        psutil.cpu_percent(None)

    def sample(self):
        now = time.time()
        dt = max(now - self._t, 0.1)
        net, disk = psutil.net_io_counters(), psutil.disk_io_counters()
        with self.lock:
            self.history["cpu"].append(psutil.cpu_percent(None))
            self.history["ram"].append(psutil.virtual_memory().percent)
            self.history["net_down"].append((net.bytes_recv - self._net.bytes_recv) / dt)
            self.history["net_up"].append((net.bytes_sent - self._net.bytes_sent) / dt)
            if disk and self._disk:
                self.history["disk_read"].append((disk.read_bytes - self._disk.read_bytes) / dt)
                self.history["disk_write"].append((disk.write_bytes - self._disk.write_bytes) / dt)
        self._net, self._disk, self._t = net, disk, now

    def run_forever(self, stop: threading.Event):
        while not stop.is_set():
            try:
                self.sample()
            except Exception:
                pass
            stop.wait(5)

    def _drives(self) -> list[dict]:
        out, seen = [], set()
        for p in psutil.disk_partitions(all=False):
            if "cdrom" in p.opts or not p.fstype:
                continue
            mount = p.mountpoint
            key = mount.rstrip("\\/").upper()
            if self.drives and key not in self.drives:
                continue
            if key in seen or mount.startswith(("/snap", "/boot", "/proc", "/sys", "/dev")):
                continue
            seen.add(key)
            try:
                u = psutil.disk_usage(mount)
            except (PermissionError, OSError):
                continue
            if u.total < 1024 ** 3:  # skip tiny system/recovery partitions
                continue
            out.append({"mount": mount, "fstype": p.fstype, "total": u.total, "used": u.used, "free": u.free, "percent": u.percent})
        return out

    def _plex_processes(self) -> list[dict]:
        procs = []
        for p in psutil.process_iter(["name", "memory_info"]):
            name = (p.info.get("name") or "").lower().removesuffix(".exe")
            if name.startswith(PLEX_PROCS):
                try:
                    procs.append({
                        "name": p.info["name"],
                        "cpu": p.cpu_percent(None) / psutil.cpu_count(),
                        "ram": p.info["memory_info"].rss if p.info.get("memory_info") else 0,
                    })
                except psutil.Error:
                    pass
        return procs

    def _gpu(self) -> list[dict]:
        if not self._gpu_cmd:
            return []
        try:
            out = subprocess.run(
                [self._gpu_cmd, "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,encoder.stats.sessionCount",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            ).stdout
        except Exception:
            return []
        gpus = []
        for line in out.strip().splitlines():
            f = [x.strip() for x in line.split(",")]
            if len(f) >= 5:
                gpus.append({"name": f[0], "util": _num(f[1]), "mem_used": _num(f[2]), "mem_total": _num(f[3]),
                             "temp": _num(f[4]), "encoder_sessions": _num(f[5]) if len(f) > 5 else None})
        return gpus

    def _temps(self) -> float | None:
        fn = getattr(psutil, "sensors_temperatures", None)
        if not fn:
            return None  # not available on Windows
        try:
            temps = fn()
        except Exception:
            return None
        for key in ("coretemp", "k10temp", "cpu_thermal", "zenpower"):
            if temps.get(key):
                return max(t.current for t in temps[key])
        return None

    def snapshot(self) -> dict:
        vm, sw = psutil.virtual_memory(), psutil.swap_memory()
        freq = psutil.cpu_freq()
        with self.lock:
            hist = {k: list(v) for k, v in self.history.items()}
        return {
            "cpu": {"percent": hist["cpu"][-1] if hist["cpu"] else psutil.cpu_percent(None),
                    "cores": psutil.cpu_count(logical=False), "threads": psutil.cpu_count(),
                    "mhz": round(freq.current) if freq else None, "temp": self._temps()},
            "ram": {"total": vm.total, "used": vm.total - vm.available, "percent": vm.percent,
                    "swap_used": sw.used, "swap_total": sw.total},
            "drives": self._drives(),
            "net": {"down": hist["net_down"][-1] if hist["net_down"] else 0, "up": hist["net_up"][-1] if hist["net_up"] else 0},
            "disk_io": {"read": hist["disk_read"][-1] if hist["disk_read"] else 0,
                        "write": hist["disk_write"][-1] if hist["disk_write"] else 0},
            "uptime": time.time() - psutil.boot_time(),
            "plex_processes": self._plex_processes(),
            "gpu": self._gpu(),
            "history": hist,
        }


def _num(x):
    try:
        return float(x)
    except ValueError:
        return None
