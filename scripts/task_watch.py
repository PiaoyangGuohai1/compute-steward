#!/usr/bin/env python3
"""
task_watch.py — Job Health & Bottleneck Inspector (compute-steward).

Diagnoses the 4 questions medical & biology researchers always ask:
  1. "Is my task still running or did it silently die?"
  2. "Did it get OOM-killed?"
  3. "Why is it so slow? Is it stuck/deadlocked (0% CPU) or only using 1 core?"
  4. "Did I accidentally cause a BLAS thread explosion?"

Usage:
  python3 scripts/task_watch.py                               # Inspect local running jobs
  python3 scripts/task_watch.py --host gpu-node1              # Inspect jobs on remote SSH host
  python3 scripts/task_watch.py --hosts local,gpu-node1,node2
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

PROBE_SCRIPT = Path(__file__).resolve().parent / "probe.py"


def classify_job(job: dict, cores: int, sibling_count: int = 1) -> tuple[str, str]:
    cpu = job.get("cpu_pct", 0.0)
    elapsed = job.get("elapsed_sec", 0)
    procs = max(job.get("proc_count", 1), sibling_count)
    mem_gb = job.get("mem_gb", 0.0)
    mem_max = job.get("mem_max_gb")

    if mem_max and mem_gb >= 0.92 * mem_max:
        return (
            "🔴 NEAR OOM LIMIT",
            f"Using {mem_gb:.1f}G of {mem_max:.1f}G cap (>=92%). High risk of Exit 137 OOM kill!",
        )
    if elapsed >= 60 and cpu < 1.0 and job.get("vram_gb", 0.0) < 0.2:
        return (
            "🔴 STALLED / HUNG (0% CPU)",
            "Process has been alive >60s with <1% CPU and no GPU activity. Check for DataLoader deadlock, network hang, or stdin prompt.",
        )
    if procs > cores * 2:
        return (
            "🟠 THREAD / WORKER THRASHING",
            f"Spawned {procs} processes on a {cores}-core machine. Set OMP_NUM_THREADS=1 and reduce worker count.",
        )
    if elapsed >= 30 and 85.0 <= cpu <= 115.0 and cores >= 8 and procs == 1:
        return (
            "🟡 SINGLE-CORE BOTTLENECK",
            f"Pegged at ~1 core ({cpu:.0f}% CPU) on a {cores}-core machine. Consider ProcessPoolExecutor / joblib parallelism.",
        )
    if cpu >= 140.0 or job.get("vram_gb", 0.0) >= 0.5 or sibling_count > 1:
        return (
            "🟢 HEALTHY (Active Compute)",
            f"Actively computing ({cpu:.1f}% CPU, {procs} worker proc(s), {mem_gb:.2f}G RAM).",
        )
    return (
        "🟢 RUNNING (Light / I/O)",
        f"Running normally ({cpu:.1f}% CPU, {mem_gb:.2f}G RAM, elapsed {elapsed}s).",
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Inspect and diagnose running scientific compute jobs")
    ap.add_argument("--host", type=str, default="", help="Single SSH host alias")
    ap.add_argument("--hosts", type=str, default="local", help="Comma-separated host aliases")
    ap.add_argument("--json", action="store_true", help="Output raw JSON diagnostics")
    args = ap.parse_args()

    targets = args.host if args.host else args.hosts
    r = subprocess.run([sys.executable, str(PROBE_SCRIPT), "--hosts", targets], capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        print(f"Error probing hosts ({targets}): {r.stderr}", file=sys.stderr)
        return 1

    raw = json.loads(r.stdout)
    snapshots = raw if isinstance(raw, list) else [raw]

    if args.json:
        for s in snapshots:
            if s.get("ok") is not False:
                jobs = s.get("jobs", [])
                title_counts = Counter(j.get("title", "") for j in jobs)
                for j in jobs:
                    status, advice = classify_job(j, s["cpu"]["cores"], title_counts[j.get("title", "")])
                    j["health_status"] = status
                    j["health_advice"] = advice
        print(json.dumps(snapshots, ensure_ascii=False, indent=2))
        return 0

    print("=" * 88)
    print(" COMPUTE STEWARD — ACTIVE TASK HEALTH & BOTTLENECK WATCHER")
    print("=" * 88)

    for s in snapshots:
        alias = s.get("alias") or s.get("host", "local")
        if s.get("ok") is False:
            print(f"\n[❌ {alias}] Unreachable: {s.get('error')}")
            continue

        cores = s["cpu"]["cores"]
        sl = s.get("vec_slice", {})
        oom_kills = sl.get("oom_kill", 0)
        jobs = s.get("jobs", [])
        title_counts = Counter(j.get("title", "") for j in jobs)

        print(f"\n🖥️  HOST: {alias} ({cores} cores · {s['mem']['avail_gb']}G avail RAM · Slice OOM count: {oom_kills})")
        if oom_kills > 0:
            print(f"   💀 ALERT: CGroup recorded {oom_kills} OOM Kill event(s) on {alias}! If a previous job vanished, it ran out of memory.")

        if not jobs:
            print("   (No active Python / R / Julia / Bioinformatics jobs owned by you on this host)")
            continue

        for j in jobs:
            status, advice = classify_job(j, cores, title_counts[j.get("title", "")])
            slice_badge = f"slice:{j['slice']}" if j.get("in_vec_slice") else "⚠️ UNCONTAINED (outside slice)"
            print(f"   • PID {j['pid']:<7d} | {status} | [{slice_badge}]")
            print(f"     Cmd    : {j['title']}")
            print(
                f"     Metrics: CPU {j['cpu_pct']:6.1f}% | RAM {j['mem_gb']:5.2f}G"
                + (f" / {j['mem_max_gb']}G cap" if j.get("mem_max_gb") else "")
                + (f" | VRAM {j['vram_gb']:.1f}G" if j.get("vram_gb", 0) > 0 else "")
                + f" | Procs: {max(j['proc_count'], title_counts[j.get('title', '')])} | Elapsed: {j['elapsed_sec']}s"
            )
            print(f"     Diagnose: {advice}")

    print("\n" + "=" * 88)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
