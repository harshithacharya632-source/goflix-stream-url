"""Real CPU usage for the "Server status" gauge.

os.getloadavg() was a poor fit: it is a 1-minute average of runnable tasks (not a percentage),
it reacts slowly, and inside containers / micro-VMs it often reads about 0 while the server is
busy. This measures CPU time actually used, over the last few seconds.

Sources, best first (the first one that works on this machine is used):
  cgroup    this container's own CPU time, as a share of its CPU limit (when a limit is set)
  proc_stat whole-machine busy time from /proc/stat, steal time included (hosts that
            throttle a small plan show up as "steal")
  process   CPU time of this Python process only (last resort)
"""
import asyncio
import os
import time
from collections import deque


def _read(path: str) -> str:
    with open(path) as f:
        return f.read()


def _cpus() -> float:
    try:
        return float(len(os.sched_getaffinity(0)))
    except AttributeError:
        return float(os.cpu_count() or 1)


def _cgroup():
    """(cpu seconds used, cpu seconds available) -- only when the container has a CPU limit."""
    try:  # cgroup v2
        usage = next(int(l.split()[1]) for l in _read("/sys/fs/cgroup/cpu.stat").splitlines()
                     if l.startswith("usage_usec")) / 1e6
        quota, period = _read("/sys/fs/cgroup/cpu.max").split()
        if quota == "max":
            return None
        return usage, time.monotonic() * int(quota) / int(period)
    except Exception:
        pass
    try:  # cgroup v1
        usage = int(_read("/sys/fs/cgroup/cpuacct/cpuacct.usage")) / 1e9
        quota = int(_read("/sys/fs/cgroup/cpu/cpu.cfs_quota_us"))
        period = int(_read("/sys/fs/cgroup/cpu/cpu.cfs_period_us"))
        if quota <= 0:
            return None
        return usage, time.monotonic() * quota / period
    except Exception:
        return None


def _proc_stat():
    """(busy jiffies, total jiffies) for the whole machine."""
    f = [int(x) for x in _read("/proc/stat").splitlines()[0].split()[1:]]
    f += [0] * (8 - len(f))
    user, nice, system, idle, iowait, irq, softirq, steal = f[:8]
    total = user + nice + system + idle + iowait + irq + softirq + steal
    return total - idle - iowait, total


def _process():
    return time.process_time(), time.monotonic() * _cpus()


_SOURCES = (("cgroup", _cgroup), ("proc_stat", _proc_stat), ("process", _process))


class CpuSampler:
    """Samples every second; `percent` is the average over the last ~6 seconds (0-100)."""

    def __init__(self, interval: float = 1.0, window: int = 6):
        self.interval = interval
        self.percent = None   # None until the first two samples exist (about 1 second after start)
        self.source = None
        self._hist = deque(maxlen=window)
        self._task = None

    def sample(self) -> None:
        for name, fn in _SOURCES:
            try:
                cur = fn()
            except Exception:
                cur = None
            if cur is None:
                continue
            if self.source != name:          # source changed: old numbers are not comparable
                self._hist.clear()
                self.source = name
            self._hist.append(cur)
            break
        if len(self._hist) >= 2:
            (b0, t0), (b1, t1) = self._hist[0], self._hist[-1]
            if t1 > t0:
                self.percent = round(max(0.0, min(100.0, (b1 - b0) / (t1 - t0) * 100)), 1)

    async def _run(self):
        while True:
            self.sample()
            await asyncio.sleep(self.interval)

    def start(self):
        if self._task is None:
            self.sample()
            self._task = asyncio.ensure_future(self._run())

    def stop(self):
        if self._task is not None:
            self._task.cancel()
            self._task = None


cpu_sampler = CpuSampler()
