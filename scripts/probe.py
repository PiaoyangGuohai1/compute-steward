#!/usr/bin/env python3
"""
Universal Zero-Dependency Compute Resource Probe & Allocator (compute-steward).

Works on:
  - Linux (workstations, HPC nodes, unified-memory GPUs, multi-GPU servers, systemd cgroup v2 slices)
  - macOS (Apple Silicon M1/M2/M3/M4 & Intel Macs via sysctl / vm_stat / ps / df)
  - Windows (via ctypes GlobalMemoryStatusEx / shutil.disk_usage / NVML)
  - Remote SSH hosts (streams this file over `ssh <host> python3 -` with zero remote installation!)

Usage:
  python3 probe.py --pretty                                     # Human-readable health card + allocation advice
  python3 probe.py --hosts local,gpu-node1,lab-server --pretty  # Multi-host comparison table & routing recommendation
  python3 probe.py --recommend --task-mem-gb 6                  # Calculate safe workers & thread caps for a 6GB/worker task
  python3 probe.py --stream --interval 2.0                      # Continuous NDJSON stream (for MenuBar / Web dashboards)
  python3 probe.py --discover-ssh                               # List SSH host aliases from ~/.ssh/config
"""
from __future__ import annotations

import argparse
import concurrent.futures
import ctypes
import getpass
import json
import math
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

GB = 1024.0 ** 3
MB = 1024.0 ** 2
MY_UID = os.getuid() if hasattr(os, "getuid") else -1
MY_USER = getpass.getuser()
MY_PID = os.getpid()

# Optional comma-separated list of background systemd service units to ignore when scanning user slices
EXTRA_IGNORE_SERVICES = {
    s.strip() for s in os.environ.get("STEWARD_IGNORE_SERVICES", "").split(",") if s.strip()
}

IGNORE_COMMS = {
    "systemd", "(sd-pam)", "sshd", "sshd-session", "bash", "zsh", "sh", "fish",
    "tmux", "tmux: server", "node", "tailscaled", "rsync", "scp", "sftp-server",
    "git", "gpg-agent", "dirmngr", "dbus-daemon", "ps", "top", "htop", "btop",
    "login", "launchd", "WindowServer", "kernel_task", "mds", "mds_stores",
}

COMPUTE_BINARIES = (
    "python", "R", "Rscript", "r", "rscript", "julia", "java", "nextflow", "snakemake",
    "star", "STAR", "cellranger", "spaceranger", "bwa", "samtools", "bcftools", "bedtools",
    "gatk", "salmon", "kallisto", "minimap2", "bowtie2", "hisat2", "fastp", "plink", "gcta",
    "ray", "Ray", "torchrun",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def read_text(path: str | Path) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def read_int(path: str | Path) -> int | None:
    txt = read_text(path)
    if not txt:
        return None
    s = txt.strip()
    if s == "max" or not s.isdigit():
        return None
    val = int(s)
    # Guard against UINT64_MAX ("infinity") values on older kernels/cgroups
    if val >= (1 << 50):
        return None
    return val


def read_kv(path: str | Path) -> dict[str, int]:
    txt = read_text(path)
    out: dict[str, int] = {}
    if not txt:
        return out
    for line in txt.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            out[parts[0]] = int(parts[1])
    return out


def run_cmd(argv: list[str], timeout: float = 4.0) -> str:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def detect_link_type() -> str:
    conn = os.environ.get("SSH_CONNECTION", "")
    parts = conn.split()
    if len(parts) >= 3:
        srv_ip = parts[2]
        if srv_ip == "127.0.0.1" or srv_ip.startswith("100."):
            return "Tailscale"
        if srv_ip.startswith(("192.168.", "10.", "172.")):
            return f"LAN ({srv_ip})"
        return f"SSH ({srv_ip})"
    return "Local"


# ---------------------------------------------------------------------------
# GPU Reader: in-process NVML (Linux/Windows) + Apple Silicon Unified Memory
# ---------------------------------------------------------------------------
class _NvmlUtil(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class _NvmlMemory(ctypes.Structure):
    _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]


class _NvmlProcInfo(ctypes.Structure):
    _fields_ = [
        ("pid", ctypes.c_uint),
        ("usedGpuMemory", ctypes.c_ulonglong),
        ("gpuInstanceId", ctypes.c_uint),
        ("computeInstanceId", ctypes.c_uint),
    ]


class GPUReader:
    def __init__(self):
        self.ok = False
        self.lib = None
        self.devices = []
        self.gpu_name = "No Dedicated GPU"
        self.apple_gpu_name = None

        if sys.platform == "darwin":
            brand = run_cmd(["sysctl", "-n", "machdep.cpu.brand_string"]).strip()
            if "Apple" in brand:
                self.apple_gpu_name = f"{brand} (Unified Memory)"
            return

        for libname in ("libnvidia-ml.so.1", "libnvidia-ml.so", "nvml.dll"):
            try:
                lib = ctypes.CDLL(libname)
                if lib.nvmlInit_v2() == 0:
                    cnt = ctypes.c_uint(0)
                    if lib.nvmlDeviceGetCount_v2(ctypes.byref(cnt)) == 0 and cnt.value > 0:
                        raw_devs = []
                        for idx in range(cnt.value):
                            dev = ctypes.c_void_p()
                            if lib.nvmlDeviceGetHandleByIndex_v2(idx, ctypes.byref(dev)) == 0:
                                buf = ctypes.create_string_buffer(96)
                                name = "NVIDIA GPU"
                                if lib.nvmlDeviceGetName(dev, buf, 96) == 0:
                                    name = buf.value.decode("utf-8", errors="replace")
                                m = _NvmlMemory()
                                total_b = int(m.total) if lib.nvmlDeviceGetMemoryInfo(dev, ctypes.byref(m)) == 0 else 0
                                raw_devs.append((idx, dev, name, total_b))
                        if raw_devs:
                            # If server has heavy compute GPUs (>=16 GiB) plus a small display adapter (<=8 GiB),
                            # filter out the display adapter so metrics reflect compute cards.
                            heavy = [d for d in raw_devs if d[3] >= int(16 * GB)]
                            self.devices = heavy if heavy else raw_devs
                            self.lib = lib
                            self.ok = True
                            first_name = self.devices[0][2]
                            self.gpu_name = f"{len(self.devices)}× {first_name}" if len(self.devices) > 1 else first_name
                            break
            except Exception:
                continue

    def sample(self) -> dict:
        if sys.platform == "darwin" and self.apple_gpu_name:
            return {
                "ok": True,
                "kind": "apple_unified",
                "name": self.apple_gpu_name,
                "count": 1,
                "util_pct": 0,
                "temp_c": 0,
                "power_w": 0.0,
                "vram_total_gb": 0.0,
                "vram_used_gb": 0.0,
                "vram_free_gb": 0.0,
                "proc_vram_bytes": {},
                "cards": [],
            }
        if not self.ok or not self.devices:
            return {
                "ok": False,
                "kind": "none",
                "name": self.gpu_name,
                "count": 0,
                "util_pct": 0,
                "temp_c": 0,
                "power_w": 0.0,
                "vram_total_gb": 0.0,
                "vram_used_gb": 0.0,
                "vram_free_gb": 0.0,
                "proc_vram_bytes": {},
                "cards": [],
            }

        proc_vram: dict[int, int] = {}
        cards = []
        total_util = 0
        max_temp = 0
        total_power = 0.0
        sum_vram_total = 0.0
        sum_vram_used = 0.0
        sum_vram_free = 0.0

        for idx, dev, name, _ in self.devices:
            u = _NvmlUtil()
            util_pct = int(u.gpu) if self.lib.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(u)) == 0 else 0

            t = ctypes.c_uint()
            temp_c = int(t.value) if self.lib.nvmlDeviceGetTemperature(dev, 0, ctypes.byref(t)) == 0 else 0

            p = ctypes.c_uint()
            power_w = round(p.value / 1000.0, 1) if self.lib.nvmlDeviceGetPowerUsage(dev, ctypes.byref(p)) == 0 else 0.0

            m = _NvmlMemory()
            if self.lib.nvmlDeviceGetMemoryInfo(dev, ctypes.byref(m)) == 0 and m.total > 0:
                vram_total_gb = round(m.total / GB, 1)
                vram_used_gb = round(m.used / GB, 1)
                vram_free_gb = round(m.free / GB, 1)
            else:
                vram_total_gb = vram_used_gb = vram_free_gb = 0.0

            card_procs: dict[int, int] = {}
            cnt = ctypes.c_uint(64)
            arr = (_NvmlProcInfo * 64)()
            ret = self.lib.nvmlDeviceGetComputeRunningProcesses_v3(dev, ctypes.byref(cnt), arr)
            if ret == 0:
                for i in range(cnt.value):
                    used = int(arr[i].usedGpuMemory)
                    if 0 < used < (1024 ** 4):
                        pid = int(arr[i].pid)
                        card_procs[pid] = card_procs.get(pid, 0) + used
                        proc_vram[pid] = proc_vram.get(pid, 0) + used

            total_util += util_pct
            max_temp = max(max_temp, temp_c)
            total_power += power_w
            sum_vram_total += vram_total_gb
            sum_vram_used += vram_used_gb
            sum_vram_free += vram_free_gb

            cards.append({
                "index": idx,
                "name": name,
                "util_pct": util_pct,
                "temp_c": temp_c,
                "power_w": power_w,
                "vram_total_gb": vram_total_gb,
                "vram_used_gb": vram_used_gb,
                "vram_free_gb": vram_free_gb,
                "proc_vram_bytes": card_procs,
            })

        n = len(cards)
        is_unified = (sum_vram_total == 0.0)
        return {
            "ok": True,
            "kind": "nvidia_unified" if is_unified else "nvidia_dedicated",
            "name": self.gpu_name,
            "count": n,
            "util_pct": int(round(total_util / n)) if n else 0,
            "temp_c": max_temp,
            "power_w": round(total_power, 1),
            "vram_total_gb": round(sum_vram_total, 1),
            "vram_used_gb": round(sum_vram_used, 1),
            "vram_free_gb": round(sum_vram_free, 1),
            "proc_vram_bytes": proc_vram,
            "cards": cards,
        }


# ---------------------------------------------------------------------------
# Disk & Mount Discovery (/, /tmp, /data, /alldata, /scratch, Home)
# ---------------------------------------------------------------------------
def probe_disks() -> dict:
    mounts = []
    seen_devs = set()
    candidates = [
        ("/", "root"),
        ("/data", "data"),
        ("/alldata", "alldata"),
        ("/scratch", "scratch"),
        (str(Path.home()), "home"),
        ("/tmp", "tmp"),
    ]
    for path_str, label in candidates:
        if not os.path.isdir(path_str):
            continue
        try:
            st = os.stat(path_str)
            usage = shutil.disk_usage(path_str)
            sig = (st.st_dev, usage.total // (1024 ** 2))
            if sig in seen_devs and label in ("home", "tmp"):
                continue
            seen_devs.add(sig)
            mounts.append({
                "mount": path_str,
                "label": label,
                "free_gb": round(usage.free / GB, 1),
                "total_gb": round(usage.total / GB, 1),
                "used_pct": round((usage.total - usage.free) / max(1, usage.total) * 100.0, 1),
            })
        except OSError:
            continue

    root_m = mounts[0] if mounts else {"free_gb": 0.0, "total_gb": 0.0}
    sec_m = next((m for m in mounts[1:] if m["label"] in ("data", "alldata", "scratch")), None)
    return {
        "free_tb": round(root_m["free_gb"] / 1024.0, 2),
        "total_tb": round(root_m["total_gb"] / 1024.0, 2),
        "free_gb": root_m["free_gb"],
        "total_gb": root_m["total_gb"],
        "data_mount": sec_m["mount"] if sec_m else None,
        "data_free_gb": sec_m["free_gb"] if sec_m else None,
        "data_total_gb": sec_m["total_gb"] if sec_m else None,
        "mounts": mounts,
    }


# ---------------------------------------------------------------------------
# Allocation Recommendation Engine (40% - 60% Idle Sweet Spot)
# ---------------------------------------------------------------------------
def compute_recommendations(payload: dict, task_mem_gb: float = 4.0) -> dict:
    cores = payload["cpu"]["cores"]
    cpu_pct = payload["cpu"]["pct"]
    load1 = payload["cpu"]["load1"]

    busy_cores = max(load1, cores * (cpu_pct / 100.0))
    idle_cores = max(0.0, cores - busy_cores)

    # Effective available memory considers both system MemAvailable and active cgroup slice limit
    sys_avail_gb = payload["mem"]["avail_gb"]
    sys_total_gb = payload["mem"]["total_gb"]
    swap_total_gb = payload["mem"]["swap_total_gb"]

    sl = payload.get("vec_slice", {})
    if sl.get("active") and sl.get("has_limit"):
        slice_headroom_gb = max(0.0, sl["high_gb"] - sl["real_gb"])
        eff_avail_gb = min(sys_avail_gb, slice_headroom_gb)
    else:
        eff_avail_gb = sys_avail_gb

    # Safety margin: larger if swap is tiny (<= 4 GB)
    small_swap = (swap_total_gb <= 4.0)
    margin_gb = max(6.0, round(sys_total_gb * (0.18 if small_swap else 0.12), 1))
    usable_mem_gb = max(1.0, round(eff_avail_gb - margin_gb, 1))

    # Sweet-spot 50% of idle cores (bounded by 40%..60%)
    target_cores = max(1, int(round(idle_cores * 0.50)))
    max_safe_cores = max(1, int(math.floor(idle_cores * 0.65)))

    workers_by_mem = max(1, int(math.floor(usable_mem_gb / max(0.5, task_mem_gb))))
    rec_workers = max(1, min(target_cores, workers_by_mem))

    if rec_workers >= 4:
        threads_per_worker = 1 if (rec_workers * 2 > max_safe_cores) else 2
    elif rec_workers > 1:
        threads_per_worker = max(1, min(4, int(max_safe_cores // rec_workers)))
    else:
        threads_per_worker = max(1, min(8, target_cores))

    # Best GPU card selection (distinguishing NVIDIA dedicated, NVIDIA unified, and Apple Silicon unified)
    best_gpu_idx = None
    best_gpu_reason = "CPU-only / No dedicated idle GPU needed"
    gpu = payload.get("gpu", {})
    cards = gpu.get("cards", [])
    if gpu.get("kind") == "apple_unified":
        best_gpu_idx = None
        best_gpu_reason = f"Apple Silicon Unified Memory ({gpu.get('name')}) — use MPS/CPU (no CUDA)"
    elif cards:
        idle_cards = [
            c for c in cards
            if c.get("util_pct", 0) < 25 and (c.get("vram_free_gb", 0) >= 8.0 or c.get("vram_total_gb", 0) == 0)
        ]
        if idle_cards:
            idle_cards.sort(key=lambda c: (-c.get("vram_free_gb", 0), c.get("util_pct", 0)))
            chosen = idle_cards[0]
            best_gpu_idx = chosen["index"]
            vram_note = f"{chosen['vram_free_gb']}G free" if chosen.get("vram_total_gb", 0) > 0 else "Unified RAM"
            best_gpu_reason = f"GPU {chosen['index']} ({chosen['name']}) is idle ({chosen['util_pct']}% util, {vram_note})"
        else:
            best_gpu_reason = "All GPUs currently busy (>=25% util or <8G free VRAM)"
    elif gpu.get("ok") and gpu.get("kind") == "nvidia_unified":
        if gpu.get("util_pct", 0) < 35:
            best_gpu_idx = 0
            best_gpu_reason = f"Unified NVIDIA GPU ({gpu.get('name')}) available ({gpu.get('util_pct', 0)}% util)"

    warnings = []
    if eff_avail_gb < 8.0:
        warnings.append(f"LOW MEMORY: Only {eff_avail_gb:.1f} GB effectively available (high OOM risk).")
    if small_swap and sys_total_gb >= 32.0:
        warnings.append(f"SMALL SWAP ({swap_total_gb:.1f} GB): Never exceed {usable_mem_gb:.0f} GB or kernel OOM killer will trigger immediately.")
    if sl.get("oom_kill", 0) > 0:
        warnings.append(f"PAST CGROUP OOM DETECTED: slice has {sl['oom_kill']} recorded oom_kill event(s).")
    for m in payload.get("disk", {}).get("mounts", []):
        if m["free_gb"] < 15.0:
            warnings.append(f"LOW DISK SPACE on {m['mount']}: only {m['free_gb']} GB free.")
    for j in payload.get("jobs", []):
        if payload["os"] == "linux" and sl.get("active") and sl.get("has_limit") and not j.get("in_vec_slice"):
            warnings.append(f"UNCONTAINED JOB (PID {j['pid']} '{j['title']}'): running outside cgroup slice without memory cap.")

    return {
        "idle_cores_est": round(idle_cores, 1),
        "effective_avail_mem_gb": round(eff_avail_gb, 1),
        "safe_mem_budget_gb": usable_mem_gb,
        "safety_margin_gb": margin_gb,
        "assumed_task_mem_gb": task_mem_gb,
        "recommended_workers": rec_workers,
        "threads_per_worker": threads_per_worker,
        "recommended_cuda_device": best_gpu_idx,
        "gpu_advice": best_gpu_reason,
        "env_exports": (
            f"OMP_NUM_THREADS={threads_per_worker} OPENBLAS_NUM_THREADS={threads_per_worker} "
            f"MKL_NUM_THREADS={threads_per_worker} NUMEXPR_NUM_THREADS={threads_per_worker}"
            + (f" CUDA_VISIBLE_DEVICES={best_gpu_idx}" if best_gpu_idx is not None else "")
        ),
        "safe_run_cmd": (
            f"scripts/safe_run.sh -m {int(max(4, min(usable_mem_gb, rec_workers * task_mem_gb + 4)))}G "
            f"-w {rec_workers} -t {threads_per_worker} -- <your_command>"
        ),
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Host Probe Implementation (Linux, macOS, Windows)
# ---------------------------------------------------------------------------
def shorten_path(s: str) -> str:
    home = os.path.expanduser("~").rstrip("/")
    for pfx, rep in [
        (f"{home}/", "~/"),
        ("/data/users/", "data/"),
        ("/alldata/users/", "alldata/"),
        ("/alldata/", "alldata/"),
    ]:
        if s.startswith(pfx):
            return rep + s[len(pfx):]
    return s


def format_job_title(args: list[str], comm: str, pid: int) -> tuple[str, str]:
    if not args:
        return comm, comm
    full_cmd = " ".join(args)
    i = 0
    if os.path.basename(args[0]) == "env":
        i += 1
        while i < len(args) and "=" in args[i] and not args[i].startswith("-"):
            i += 1
    rem = args[i:] if i < len(args) else args
    if not rem:
        return comm, full_cmd
    exe = os.path.basename(rem[0])
    if exe.startswith("python"):
        j = 1
        while j < len(rem) and rem[j].startswith("-") and rem[j] not in ("-", "-c", "-m"):
            j += 1
        if j < len(rem):
            target = rem[j]
            if target == "-":
                try:
                    cwd = shorten_path(os.readlink(f"/proc/{pid}/cwd"))
                except OSError:
                    cwd = ""
                return (f"python - [{cwd}]" if cwd else "python - (stdin)"), full_cmd
            if target == "-c":
                snippet = rem[j + 1].strip().splitlines()[0][:40] if j + 1 < len(rem) else ""
                return f'python -c "{snippet}"', full_cmd
            if target == "-m" and j + 1 < len(rem):
                mod_rest = " ".join(shorten_path(x) for x in rem[j + 1:j + 4])
                return f"python -m {mod_rest}", full_cmd
            return " ".join(shorten_path(x) for x in rem[j:j + 4]), full_cmd
    return " ".join(shorten_path(x) for x in rem[:4]), full_cmd


def _is_background_service_unit(unit_name: str) -> bool:
    if unit_name in EXTRA_IGNORE_SERVICES:
        return True
    # Systemd .service units inside user slices are typically long-running daemons/proxies/dashboards,
    # whereas interactive compute jobs launched via systemd-run --scope create .scope units.
    if unit_name.endswith(".service"):
        return True
    return False


class HostProbe:
    def __init__(self):
        self.os_name = "darwin" if sys.platform == "darwin" else ("windows" if os.name == "nt" else "linux")
        self.hostname = socket.gethostname()
        self.cores = os.cpu_count() or 4
        self.gpu_reader = GPUReader()
        self.link_type = detect_link_type()
        self.clk_tck = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") and "SC_CLK_TCK" in os.sysconf_names else 100
        self.page_size = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") and "SC_PAGE_SIZE" in os.sysconf_names else 4096

        self.user_cg_root = f"/sys/fs/cgroup/user.slice/user-{MY_UID}.slice/user@{MY_UID}.service"
        self.prev_cpu_total = 0
        self.prev_cpu_idle = 0
        self.prev_scope_usec: dict[str, int] = {}
        self.prev_pid_cpusec: dict[int, float] = {}
        self.prev_mono = time.monotonic()

        if self.os_name == "linux":
            self.prev_cpu_total, self.prev_cpu_idle = self._linux_cpu_jiffies()
            self._init_linux_deltas()

    def _linux_cpu_jiffies(self) -> tuple[int, int]:
        txt = read_text("/proc/stat")
        if not txt:
            return 0, 0
        first = txt.splitlines()[0].split()
        vals = [int(x) for x in first[1:9]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        return sum(vals), idle

    def _linux_proc_info(self, pid: int, uptime_sec: float) -> dict | None:
        try:
            uid = os.stat(f"/proc/{pid}").st_uid
        except OSError:
            return None
        stat_txt = read_text(f"/proc/{pid}/stat")
        if not stat_txt:
            return None
        rparen = stat_txt.rfind(")")
        if rparen < 0:
            return None
        comm = stat_txt[stat_txt.find("(") + 1:rparen]
        rest = stat_txt[rparen + 2:].split()
        if len(rest) < 22:
            return None
        utime, stime, starttime, rss_pages = int(rest[11]), int(rest[12]), int(rest[19]), int(rest[21])
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                raw = f.read()
            args = [a.decode("utf-8", errors="replace") for a in raw.split(b"\x00") if a]
        except OSError:
            args = []
        return {
            "pid": pid,
            "uid": uid,
            "comm": comm,
            "args": args,
            "cpu_sec": (utime + stime) / self.clk_tck,
            "rss_bytes": rss_pages * self.page_size,
            "elapsed_sec": max(0.0, uptime_sec - (starttime / self.clk_tck)),
        }

    def _init_linux_deltas(self):
        if os.path.isdir(self.user_cg_root):
            for top_entry in ("compute.slice", "vec.slice", "agent_runs.slice"):
                sp = os.path.join(self.user_cg_root, top_entry)
                if os.path.isdir(sp):
                    for r, _, _ in os.walk(sp):
                        u = read_kv(os.path.join(r, "cpu.stat")).get("usage_usec")
                        if u is not None:
                            self.prev_scope_usec[r] = u
        try:
            up_txt = read_text("/proc/uptime")
            up0 = float(up_txt.split()[0]) if up_txt else 0.0
            for d in os.listdir("/proc"):
                if d.isdigit():
                    pi0 = self._linux_proc_info(int(d), up0)
                    if pi0 and pi0["uid"] == MY_UID:
                        self.prev_pid_cpusec[int(d)] = pi0["cpu_sec"]
        except OSError:
            pass

    def sample(self, task_mem_gb: float = 4.0) -> dict:
        if self.os_name == "linux":
            payload = self._sample_linux()
        elif self.os_name == "darwin":
            payload = self._sample_macos()
        else:
            payload = self._sample_windows_or_fallback()
        payload["ok"] = True
        payload["recommendation"] = compute_recommendations(payload, task_mem_gb=task_mem_gb)
        return payload

    def _sample_linux(self) -> dict:
        now_mono = time.monotonic()
        dt = max(0.05, now_mono - self.prev_mono)
        self.prev_mono = now_mono

        cur_total, cur_idle = self._linux_cpu_jiffies()
        d_total = max(1, cur_total - self.prev_cpu_total)
        d_idle = max(0, cur_idle - self.prev_cpu_idle)
        cpu_pct = round(max(0.0, min(100.0, (d_total - d_idle) / d_total * 100.0)), 1)
        self.prev_cpu_total, self.prev_cpu_idle = cur_total, cur_idle

        up_txt = read_text("/proc/uptime")
        uptime_sec = float(up_txt.split()[0]) if up_txt else 0.0
        ld_txt = read_text("/proc/loadavg")
        l1, l5, l15 = ([float(x) for x in ld_txt.split()[:3]] if ld_txt else [0.0, 0.0, 0.0])

        # Memory
        txt = read_text("/proc/meminfo") or ""
        kv = {}
        for line in txt.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                kv[parts[0].rstrip(":")] = int(parts[1]) * 1024
        total = kv.get("MemTotal", 0)
        avail = kv.get("MemAvailable", 0)
        anon = kv.get("AnonPages", 0)
        cache = kv.get("Cached", 0) + kv.get("Buffers", 0) + kv.get("SReclaimable", 0)
        swap_total = kv.get("SwapTotal", 0)
        swap_free = kv.get("SwapFree", 0)
        mem = {
            "total_gb": round(total / GB, 1),
            "avail_gb": round(avail / GB, 1),
            "used_gb": round(max(0, total - avail) / GB, 1),
            "anon_gb": round(anon / GB, 1),
            "cache_gb": round(cache / GB, 1),
            "swap_used_gb": round(max(0, swap_total - swap_free) / GB, 1),
            "swap_total_gb": round(swap_total / GB, 1),
        }

        gpu = self.gpu_reader.sample()
        proc_vram = gpu["proc_vram_bytes"]

        # Cgroup slice discovery (supports compute.slice or vec.slice)
        active_slice_dir = os.path.join(self.user_cg_root, "vec.slice")
        if not os.path.isdir(active_slice_dir) and os.path.isdir(os.path.join(self.user_cg_root, "compute.slice")):
            active_slice_dir = os.path.join(self.user_cg_root, "compute.slice")

        slice_exists = os.path.isdir(active_slice_dir)
        slice_cur = read_int(os.path.join(active_slice_dir, "memory.current")) or 0
        raw_high = read_int(os.path.join(active_slice_dir, "memory.high"))
        raw_max = read_int(os.path.join(active_slice_dir, "memory.max"))
        slice_has_limit = (raw_high is not None) or (raw_max is not None)
        slice_high = raw_high or (raw_max if raw_max is not None else int(total * 0.75))
        slice_max = raw_max or (raw_high if raw_high is not None else int(total * 0.80))
        slice_swap = read_int(os.path.join(active_slice_dir, "memory.swap.current")) or 0
        slice_stat = read_kv(os.path.join(active_slice_dir, "memory.stat"))
        slice_events = read_kv(os.path.join(active_slice_dir, "memory.events"))
        slice_anon = slice_stat.get("anon", 0)
        slice_file = slice_stat.get("file", 0)
        slice_kernel = slice_stat.get("kernel", 0) + slice_stat.get("shmem", 0)
        slice_real = slice_anon + slice_kernel

        # Discover running jobs
        seen_pids = {MY_PID}
        jobs = []
        new_scope_usec: dict[str, int] = {}

        if os.path.isdir(self.user_cg_root):
            for top_entry in ("compute.slice", "vec.slice", "agent_runs.slice"):
                slice_path = os.path.join(self.user_cg_root, top_entry)
                if not os.path.isdir(slice_path):
                    continue
                for root, _, _ in os.walk(slice_path):
                    unit_name = os.path.basename(root)
                    if root == slice_path or unit_name.endswith(".slice"):
                        continue
                    if _is_background_service_unit(unit_name):
                        for ln in (read_text(os.path.join(root, "cgroup.procs")) or "").splitlines():
                            if ln.strip().isdigit():
                                seen_pids.add(int(ln.strip()))
                        continue
                    pids = [int(x) for x in (read_text(os.path.join(root, "cgroup.procs")) or "").splitlines() if x.strip().isdigit()]
                    if not pids:
                        continue
                    seen_pids.update(pids)
                    p_infos = [pi for pid in pids if (pi := self._linux_proc_info(pid, uptime_sec))]
                    if not p_infos:
                        continue

                    non_env = [p for p in p_infos if os.path.basename(p["args"][0] if p["args"] else p["comm"]) != "env"]
                    pool = non_env or p_infos
                    py_procs = [p for p in pool if os.path.basename(p["args"][0] if p["args"] else p["comm"]).startswith("python")]
                    lead = max(py_procs or pool, key=lambda p: p["elapsed_sec"])
                    title, full_cmd = format_job_title(lead["args"], lead["comm"], lead["pid"])

                    usec = read_kv(os.path.join(root, "cpu.stat")).get("usage_usec")
                    job_cpu_pct = 0.0
                    if usec is not None:
                        new_scope_usec[root] = usec
                        if root in self.prev_scope_usec:
                            job_cpu_pct = round(max(0, usec - self.prev_scope_usec[root]) / (dt * 1_000_000.0) * 100.0, 1)

                    u_stat = read_kv(os.path.join(root, "memory.stat"))
                    u_cur = read_int(os.path.join(root, "memory.current")) or 0
                    u_max = read_int(os.path.join(root, "memory.max"))
                    u_real = (u_stat.get("anon", 0) + u_stat.get("kernel", 0)) or u_cur
                    u_vram = sum(proc_vram.get(pid, 0) for pid in pids)

                    jobs.append({
                        "id": unit_name,
                        "pid": lead["pid"],
                        "title": title,
                        "cmd": full_cmd,
                        "slice": top_entry.replace(".slice", ""),
                        "in_vec_slice": top_entry in ("compute.slice", "vec.slice"),
                        "cpu_pct": job_cpu_pct,
                        "mem_gb": round(u_real / GB, 2),
                        "mem_max_gb": round(u_max / GB, 1) if u_max else None,
                        "vram_gb": round(u_vram / GB, 2),
                        "elapsed_sec": int(lead["elapsed_sec"]),
                        "proc_count": len(p_infos),
                    })
        self.prev_scope_usec = new_scope_usec

        # VRAM ownership split
        my_vram_bytes = other_vram_bytes = 0
        pid_is_mine: dict[int, bool] = {}
        for vpid, vbytes in proc_vram.items():
            try:
                is_mine = (os.stat(f"/proc/{vpid}").st_uid == MY_UID)
            except OSError:
                is_mine = False
            pid_is_mine[vpid] = is_mine
            if is_mine:
                my_vram_bytes += vbytes
            else:
                other_vram_bytes += vbytes

        gpu_cards_payload = []
        for c in gpu.get("cards", []):
            c_my = sum(vb for vp, vb in c["proc_vram_bytes"].items() if pid_is_mine.get(vp, False))
            c_oth = sum(vb for vp, vb in c["proc_vram_bytes"].items() if not pid_is_mine.get(vp, False))
            gpu_cards_payload.append({
                "index": c["index"],
                "name": c["name"],
                "util_pct": c["util_pct"],
                "temp_c": c["temp_c"],
                "power_w": c["power_w"],
                "vram_total_gb": c["vram_total_gb"],
                "vram_used_gb": c["vram_used_gb"],
                "vram_free_gb": c["vram_free_gb"],
                "my_vram_gb": round(c_my / GB, 1),
                "other_vram_gb": round(c_oth / GB, 1),
            })

        # Bare compute processes outside slice
        new_pid_cpusec: dict[int, float] = {}
        try:
            proc_entries = [int(d) for d in os.listdir("/proc") if d.isdigit()]
        except OSError:
            proc_entries = []

        for pid in proc_entries:
            if pid in seen_pids:
                continue
            pi = self._linux_proc_info(pid, uptime_sec)
            if not pi or pi["uid"] != MY_UID or pi["comm"] in IGNORE_COMMS:
                continue
            exe_base = os.path.basename(pi["args"][0]) if pi["args"] else pi["comm"]
            if exe_base in IGNORE_COMMS:
                continue
            joined = " ".join(pi["args"])
            if any(x in joined for x in ("/.orca-remote/", "/.vscode-server/", "/.cursor-server/", "probe.py", "dashboard_server.py")):
                continue
            if joined.startswith(("python3 -u -", "python -u -", "python3 - ")) or "/python3 -u -" in joined:
                continue
            if not any(exe_base.startswith(b) for b in COMPUTE_BINARIES):
                continue

            new_pid_cpusec[pid] = pi["cpu_sec"]
            p_cpu = round(max(0.0, pi["cpu_sec"] - self.prev_pid_cpusec.get(pid, pi["cpu_sec"])) / dt * 100.0, 1)
            title, full_cmd = format_job_title(pi["args"], pi["comm"], pid)
            jobs.append({
                "id": f"bare-{pid}",
                "pid": pid,
                "title": title,
                "cmd": full_cmd,
                "slice": "outside",
                "in_vec_slice": False,
                "cpu_pct": p_cpu,
                "mem_gb": round(pi["rss_bytes"] / GB, 2),
                "mem_max_gb": None,
                "vram_gb": round(proc_vram.get(pid, 0) / GB, 2),
                "elapsed_sec": int(pi["elapsed_sec"]),
                "proc_count": 1,
            })
        self.prev_pid_cpusec = new_pid_cpusec
        jobs.sort(key=lambda j: (-int(j["in_vec_slice"]), -j["elapsed_sec"]))

        return {
            "ts": time.time(),
            "host": self.hostname,
            "os": "linux",
            "link": self.link_type,
            "uptime_sec": int(uptime_sec),
            "cpu": {"cores": self.cores, "pct": cpu_pct, "load1": round(l1, 2), "load5": round(l5, 2), "load15": round(l15, 2)},
            "mem": mem,
            "gpu": {
                "ok": gpu["ok"],
                "kind": gpu.get("kind", "none"),
                "name": gpu["name"],
                "count": gpu.get("count", 0),
                "util_pct": gpu["util_pct"],
                "temp_c": gpu["temp_c"],
                "power_w": gpu["power_w"],
                "vram_total_gb": gpu["vram_total_gb"],
                "vram_used_gb": gpu["vram_used_gb"],
                "vram_free_gb": gpu["vram_free_gb"],
                "my_vram_gb": round(my_vram_bytes / GB, 1),
                "other_vram_gb": round(other_vram_bytes / GB, 1),
                "cards": gpu_cards_payload,
            },
            "vec_slice": {
                "active": slice_exists,
                "has_limit": slice_has_limit,
                "current_gb": round(slice_cur / GB, 2),
                "real_gb": round(slice_real / GB, 2),
                "anon_gb": round(slice_anon / GB, 2),
                "file_gb": round(slice_file / GB, 2),
                "kernel_gb": round(slice_kernel / GB, 2),
                "swap_gb": round(slice_swap / GB, 2),
                "high_gb": round(slice_high / GB, 1),
                "max_gb": round(slice_max / GB, 1),
                "oom_kill": slice_events.get("oom_kill", 0),
                "high_throttle": slice_events.get("high", 0),
            },
            "disk": probe_disks(),
            "jobs": jobs,
        }

    def _sample_macos(self) -> dict:
        l1, l5, l15 = os.getloadavg()
        boot_txt = run_cmd(["sysctl", "-n", "kern.boottime"])
        m_boot = re.search(r"sec\s*=\s*(\d+)", boot_txt)
        uptime_sec = int(time.time() - int(m_boot.group(1))) if m_boot else 0

        total_bytes = int(run_cmd(["sysctl", "-n", "hw.memsize"]).strip() or 0)
        vm = run_cmd(["vm_stat"])
        page_sz = int((re.search(r"page size of (\d+)", vm) or [0, 16384])[1])
        pages = {k.strip(): int(v.strip().rstrip(".")) for k, v in re.findall(r"^([^:\n]+):\s+(\d+)\.?$", vm, re.M)}

        free_pages = pages.get("Pages free", 0) + pages.get("Pages speculative", 0)
        cache_pages = pages.get("Pages inactive", 0) + pages.get("Pages purgeable", 0)
        wired_pages = pages.get("Pages wired down", 0) + pages.get("Pages occupied by compressor", 0)
        active_pages = pages.get("Pages active", 0)

        avail_bytes = (free_pages + cache_pages) * page_sz
        anon_bytes = (active_pages + wired_pages) * page_sz
        cache_bytes = cache_pages * page_sz

        swap_txt = run_cmd(["sysctl", "-n", "vm.swapusage"])
        sw = dict(re.findall(r"(total|used|free)\s*=\s*([\d.]+)M", swap_txt))
        swap_total_gb = round(float(sw.get("total", 0.0)) / 1024.0, 1)
        swap_used_gb = round(float(sw.get("used", 0.0)) / 1024.0, 1)

        ps_out = run_cmd(["ps", "-Ao", "pid=,pcpu=,rss=,etime=,user=,comm=,args="])
        total_pcpu = 0.0
        jobs = []
        for line in ps_out.splitlines():
            parts = line.strip().split(None, 6)
            if len(parts) < 7 or not parts[0].isdigit():
                continue
            pid = int(parts[0])
            try:
                pcpu = float(parts[1])
                rss_kb = int(parts[2])
            except ValueError:
                continue
            total_pcpu += pcpu
            etime_str, user, comm, full_args = parts[3], parts[4], parts[5], parts[6]
            if user != MY_USER or pid == MY_PID:
                continue
            exe_base = os.path.basename(comm)
            if exe_base in IGNORE_COMMS or any(x in full_args for x in ("probe.py", "dashboard_server.py", "Monitor", "Helper", ".app/Contents")):
                continue
            if not any(exe_base.startswith(b) for b in COMPUTE_BINARIES):
                continue
            title, cmd_clean = format_job_title(full_args.split(), exe_base, pid)
            jobs.append({
                "id": f"mac-{pid}",
                "pid": pid,
                "title": title,
                "cmd": cmd_clean,
                "slice": "local",
                "in_vec_slice": True,
                "cpu_pct": round(pcpu, 1),
                "mem_gb": round((rss_kb * 1024) / GB, 2),
                "mem_max_gb": None,
                "vram_gb": 0.0,
                "elapsed_sec": _parse_ps_etime(etime_str),
                "proc_count": 1,
            })

        cpu_pct = round(min(100.0, total_pcpu / max(1, self.cores)), 1)
        gpu = self.gpu_reader.sample()

        return {
            "ts": time.time(),
            "host": self.hostname,
            "os": "darwin",
            "link": self.link_type,
            "uptime_sec": uptime_sec,
            "cpu": {"cores": self.cores, "pct": cpu_pct, "load1": round(l1, 2), "load5": round(l5, 2), "load15": round(l15, 2)},
            "mem": {
                "total_gb": round(total_bytes / GB, 1),
                "avail_gb": round(avail_bytes / GB, 1),
                "used_gb": round(max(0, total_bytes - avail_bytes) / GB, 1),
                "anon_gb": round(anon_bytes / GB, 1),
                "cache_gb": round(cache_bytes / GB, 1),
                "swap_used_gb": swap_used_gb,
                "swap_total_gb": swap_total_gb,
            },
            "gpu": {
                "ok": gpu["ok"],
                "kind": gpu.get("kind", "apple_unified"),
                "name": gpu["name"],
                "count": 1,
                "util_pct": 0,
                "temp_c": 0,
                "power_w": 0.0,
                "vram_total_gb": round(total_bytes / GB, 1),
                "vram_used_gb": round(max(0, total_bytes - avail_bytes) / GB, 1),
                "vram_free_gb": round(avail_bytes / GB, 1),
                "my_vram_gb": 0.0,
                "other_vram_gb": 0.0,
                "cards": [],
            },
            "vec_slice": {
                "active": False,
                "has_limit": False,
                "current_gb": 0.0,
                "real_gb": 0.0,
                "anon_gb": 0.0,
                "file_gb": 0.0,
                "kernel_gb": 0.0,
                "swap_gb": 0.0,
                "high_gb": round(total_bytes / GB, 1),
                "max_gb": round(total_bytes / GB, 1),
                "oom_kill": 0,
                "high_throttle": 0,
            },
            "disk": probe_disks(),
            "jobs": jobs,
        }

    def _sample_windows_or_fallback(self) -> dict:
        total_gb, avail_gb, swap_total_gb, swap_used_gb = 16.0, 8.0, 4.0, 0.0
        if os.name == "nt":
            try:
                class _MEMORYSTATUSEX(ctypes.Structure):
                    _fields_ = [
                        ("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                    ]
                stat = _MEMORYSTATUSEX()
                stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
                if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                    total_gb = round(stat.ullTotalPhys / GB, 1)
                    avail_gb = round(stat.ullAvailPhys / GB, 1)
                    swap_total = max(0, stat.ullTotalPageFile - stat.ullTotalPhys)
                    swap_avail = max(0, stat.ullAvailPageFile - stat.ullAvailPhys)
                    swap_total_gb = round(swap_total / GB, 1)
                    swap_used_gb = round(max(0, swap_total - swap_avail) / GB, 1)
            except Exception:
                pass
        used_gb = round(max(0.0, total_gb - avail_gb), 1)
        gpu = self.gpu_reader.sample()
        return {
            "ts": time.time(),
            "host": self.hostname,
            "os": platform.system().lower(),
            "link": self.link_type,
            "uptime_sec": 0,
            "cpu": {"cores": self.cores, "pct": 0.0, "load1": 0.0, "load5": 0.0, "load15": 0.0},
            "mem": {
                "total_gb": total_gb,
                "avail_gb": avail_gb,
                "used_gb": used_gb,
                "anon_gb": used_gb,
                "cache_gb": 0.0,
                "swap_used_gb": swap_used_gb,
                "swap_total_gb": swap_total_gb,
            },
            "gpu": {
                "ok": gpu["ok"],
                "kind": gpu.get("kind", "none"),
                "name": gpu["name"],
                "count": gpu.get("count", 0),
                "util_pct": gpu["util_pct"],
                "temp_c": gpu["temp_c"],
                "power_w": gpu["power_w"],
                "vram_total_gb": gpu["vram_total_gb"],
                "vram_used_gb": gpu["vram_used_gb"],
                "vram_free_gb": gpu["vram_free_gb"],
                "my_vram_gb": 0.0,
                "other_vram_gb": 0.0,
                "cards": gpu.get("cards", []),
            },
            "vec_slice": {
                "active": False, "has_limit": False, "current_gb": 0.0, "real_gb": 0.0,
                "anon_gb": 0.0, "file_gb": 0.0, "kernel_gb": 0.0, "swap_gb": 0.0,
                "high_gb": total_gb, "max_gb": total_gb, "oom_kill": 0, "high_throttle": 0,
            },
            "disk": probe_disks(),
            "jobs": [],
        }


def _parse_ps_etime(s: str) -> int:
    days = 0
    if "-" in s:
        d_part, s = s.split("-", 1)
        days = int(d_part) if d_part.isdigit() else 0
    parts = [int(x) for x in s.split(":") if x.isdigit()]
    if len(parts) == 3:
        return days * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return days * 86400 + parts[0] * 60 + parts[1]
    return 0


# ---------------------------------------------------------------------------
# Multi-Host SSH Probing & SSH Config Discovery
# ---------------------------------------------------------------------------
def discover_ssh_hosts() -> list[str]:
    cfg = Path.home() / ".ssh" / "config"
    if not cfg.is_file():
        return []
    ignore = {"github.com", "gitlab.com", "bitbucket.org", "*"}
    hosts = []
    for line in (read_text(cfg) or "").splitlines():
        line = line.strip()
        if line.lower().startswith("host "):
            for token in line.split()[1:]:
                if "*" not in token and "?" not in token and token not in ignore and token not in hosts:
                    hosts.append(token)
    return hosts


def probe_remote_host(alias: str, task_mem_gb: float = 4.0, timeout: float = 12.0) -> dict:
    script_bytes = Path(__file__).read_bytes()
    cmd = [
        "ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6",
        alias, f"python3 - --task-mem-gb {task_mem_gb}"
    ]
    t0 = time.monotonic()
    try:
        r = subprocess.run(cmd, input=script_bytes, capture_output=True, timeout=timeout)
        if r.returncode == 0 and r.stdout.strip():
            last_line = r.stdout.decode("utf-8", errors="replace").strip().splitlines()[-1]
            data = json.loads(last_line)
            data["alias"] = alias
            data["rtt_ms"] = int((time.monotonic() - t0) * 1000)
            return data
        err = r.stderr.decode("utf-8", errors="replace").strip().splitlines()[-1] if r.stderr else f"Exit {r.returncode}"
        return {"alias": alias, "host": alias, "ok": False, "error": err}
    except Exception as e:
        return {"alias": alias, "host": alias, "ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Pretty Human-Readable Printer (for Researchers & AI Context)
# ---------------------------------------------------------------------------
def format_pretty_report(snapshots: list[dict]) -> str:
    lines = []
    lines.append("=" * 88)
    lines.append(" COMPUTE STEWARD — LIVE HARDWARE & SAFE PARALLELISM REPORT (40%–60% Idle Sweet Spot)")
    lines.append("=" * 88)

    for s in snapshots:
        alias = s.get("alias") or s.get("host", "local")
        if s.get("ok") is False:
            lines.append(f"\n[❌ {alias}] Unreachable: {s.get('error', 'SSH failed')}")
            continue

        cpu = s["cpu"]
        mem = s["mem"]
        gpu = s["gpu"]
        sl = s["vec_slice"]
        disk = s["disk"]
        rec = s["recommendation"]
        jobs = s["jobs"]

        lines.append(f"\n🖥️  HOST: {alias} ({s['host']} · {s['os']} · {s['link']})")
        lines.append(
            f"   ├─ CPU   : {cpu['cores']} cores | Busy: {cpu['pct']}% | Load (1/5/15m): "
            f"{cpu['load1']} / {cpu['load5']} / {cpu['load15']} | ~{rec['idle_cores_est']} idle cores"
        )
        lines.append(
            f"   ├─ Memory: {mem['avail_gb']} GB available / {mem['total_gb']} GB total "
            f"(Program Anon: {mem['anon_gb']}G · Reclaimable Cache: {mem['cache_gb']}G · Swap: {mem['swap_used_gb']}/{mem['swap_total_gb']}G)"
        )
        if sl.get("active") and (sl.get("has_limit") or sl.get("current_gb", 0) > 0.05):
            lim_str = f"{sl['high_gb']}G High / {sl['max_gb']}G Max" if sl.get("has_limit") else "No hard cap"
            lines.append(
                f"   ├─ CGroup: user slice active — Real Anon+Kernel: {sl['real_gb']}G "
                f"(+File Cache {sl['file_gb']}G) / {lim_str} | OOM Kills: {sl['oom_kill']}"
            )
        if gpu.get("ok"):
            if gpu.get("cards"):
                lines.append(
                    f"   ├─ GPU   : {gpu['name']} | Avg Util: {gpu['util_pct']}% | "
                    f"VRAM Free: {gpu['vram_free_gb']}G / {gpu['vram_total_gb']}G (Mine: {gpu['my_vram_gb']}G, Others: {gpu['other_vram_gb']}G)"
                )
                for c in gpu["cards"]:
                    lines.append(
                        f"   │          • GPU {c['index']} ({c['name']}): Util {c['util_pct']:3d}% | "
                        f"VRAM {c['vram_used_gb']:.1f}/{c['vram_total_gb']:.1f}G (Free {c['vram_free_gb']:.1f}G) | {c['temp_c']}°C"
                    )
            else:
                lines.append(f"   ├─ GPU   : {gpu['name']} | Util: {gpu['util_pct']}% | Temp: {gpu['temp_c']}°C")
        else:
            lines.append("   ├─ GPU   : None / CPU-only node")

        mounts_str = " · ".join(f"{m['mount']}: {m['free_gb']}G free ({100-m['used_pct']:.0f}%)" for m in disk.get("mounts", []))
        lines.append(f"   ├─ Disks : {mounts_str}")
        lines.append(f"   ├─ MyJobs: {len(jobs)} active compute job(s)")
        for j in jobs[:5]:
            slice_tag = j["slice"] if j["in_vec_slice"] else "⚠️ OUTSIDE-SLICE"
            lines.append(
                f"   │          • [PID {j['pid']} | {slice_tag}] {j['title']} — "
                f"CPU {j['cpu_pct']}% | Mem {j['mem_gb']}G | {j['elapsed_sec']}s"
            )

        lines.append(f"   └─ 💡 AI ALLOCATION ADVICE (assuming ~{rec['assumed_task_mem_gb']} GB/worker):")
        lines.append(
            f"        • Recommended Workers : {rec['recommended_workers']} parallel processes "
            f"(threads/worker = {rec['threads_per_worker']}, Safe Memory Cap = {rec['safe_mem_budget_gb']} GB)"
        )
        lines.append(f"        • GPU Selection       : {rec['gpu_advice']}")
        lines.append(f"        • Environment Exports : {rec['env_exports']}")
        lines.append(f"        • Safe Launch Command : {rec['safe_run_cmd']}")
        for w in rec.get("warnings", []):
            lines.append(f"        ⚠️  {w}")

    lines.append("\n" + "=" * 88)
    return "\n".join(lines)


def main() -> int:
    default_interval = float(os.environ.get("STEWARD_PROBE_INTERVAL", os.environ.get("SPARK_PROBE_INTERVAL", "2.0")))
    ap = argparse.ArgumentParser(description="Universal Zero-Dependency Compute Resource Probe")
    ap.add_argument("--stream", action="store_true", help="Continuously emit NDJSON every --interval seconds")
    ap.add_argument("--interval", type=float, default=default_interval)
    ap.add_argument("--hosts", type=str, default="local", help="Comma-separated hosts (e.g. local,gpu-node1,lab-server)")
    ap.add_argument("--discover-ssh", action="store_true", help="List SSH aliases from ~/.ssh/config and exit")
    ap.add_argument("--task-mem-gb", type=float, default=4.0, help="Estimated peak RAM (GB) per parallel worker")
    ap.add_argument("--pretty", action="store_true", help="Print human-readable table and allocation recommendation")
    ap.add_argument("--recommend", action="store_true", help="Alias for --pretty")
    args = ap.parse_args()

    if args.discover_ssh:
        print(json.dumps({"ssh_hosts": discover_ssh_hosts()}, ensure_ascii=False, indent=2))
        return 0

    if args.stream:
        probe = HostProbe()
        time.sleep(0.15)
        while True:
            if hasattr(os, "getppid") and os.name != "nt" and os.getppid() == 1:
                break
            payload = probe.sample(task_mem_gb=args.task_mem_gb)
            try:
                sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
                sys.stdout.flush()
            except (BrokenPipeError, OSError):
                break
            time.sleep(args.interval)
        return 0

    host_list = [h.strip() for h in args.hosts.split(",") if h.strip()]
    snapshots = []

    def fetch_one(h: str) -> dict:
        if h in ("local", "localhost", "mac", socket.gethostname()):
            p = HostProbe()
            time.sleep(0.15)
            d = p.sample(task_mem_gb=args.task_mem_gb)
            d["alias"] = h
            return d
        return probe_remote_host(h, task_mem_gb=args.task_mem_gb)

    if len(host_list) == 1:
        snapshots.append(fetch_one(host_list[0]))
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(host_list))) as pool:
            snapshots = list(pool.map(fetch_one, host_list))

    if args.pretty or args.recommend:
        print(format_pretty_report(snapshots))
    else:
        out = snapshots[0] if len(snapshots) == 1 else snapshots
        print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
