---
name: compute-steward
description: >-
  Scientific & biomedical compute resource steward (CPU, Memory/RAM, Swap, Disk I/O, GPU, and
  process health) for local machines (macOS/Linux/Windows) and remote SSH servers. Use when running
  or planning data-heavy tasks (single-cell RNA-seq, spatial transcriptomics, Seurat, Scanpy,
  optimal transport, numerical simulations, or ML training), when checking available hardware
  resources, preventing OOM kills or server lockups, configuring safe parallelism (40-60% idle sweet
  spot + BLAS thread caps), diagnosing stuck/frozen jobs, or building a live multi-host resource
  monitor (Web dashboard, CLI TUI, or native macOS menu bar app).
---

# Compute Steward (`compute-steward`)

A zero-dependency compute resource manager and monitoring blueprint designed for biomedical researchers, medical students, and AI coding agents. It prevents the three classic laboratory computing disasters:
1. **Hogging 100% of a shared machine** (or exploding BLAS threads so 32 workers spawn $32 \times 32 = 1024$ threads and freeze `sshd`).
2. **Silent Out-Of-Memory (OOM) kills** (confusing file cache with real memory, overflowing small swap partitions, or duplicating large `.h5ad`/`.rds` matrices across `fork` workers).
3. **Zombie / hung / single-core-bottlenecked jobs** (not knowing whether a script is actively computing, stuck on network/disk I/O, deadlocked, or crawling on 1 core).

---

## Quick Command Reference

All tools in `scripts/` use **only the Python 3 standard library** and POSIX shell—no `pip install` required on either local or remote machines.

```bash
SKILL_DIR="$(cd "$(dirname "$0")" && pwd)"   # or ~/.agents/skills/compute-steward

# 1. Probe local machine + any remote SSH hosts (CPU, RAM, Swap, Disk, GPU, my jobs)
python3 "$SKILL_DIR/scripts/probe.py" --pretty
python3 "$SKILL_DIR/scripts/probe.py" --hosts local,gpu-node1,lab-server --recommend --task-mem-gb 6

# 2. Launch a job safely (caps BLAS threads, enforces memory limit / cgroup slice / nice priority)
"$SKILL_DIR/scripts/safe_run.sh" -m 24G -w 8 -t 2 -- python3 run_pipeline.py

# 3. Diagnose running jobs (detects OOM kills, 0% CPU hangs, thread thrashing, log staleness)
python3 "$SKILL_DIR/scripts/task_watch.py"
python3 "$SKILL_DIR/scripts/task_watch.py" --host gpu-node1

# 4. Start a zero-dependency live multi-host Web Dashboard (http://127.0.0.1:8765)
python3 "$SKILL_DIR/scripts/dashboard_server.py" --hosts local,gpu-node1 --port 8765
```

---

## Core Workflow for AI Agents

Whenever the user asks to run a data analysis script, benchmark, simulation, or asks *"what resources do I have?"* / *"how many workers should I use?"* / *"is my job stuck?"*, follow these four rules:

### Rule 1: Probe First, Never Guess
Before launching any non-trivial computation, run `scripts/probe.py` against the target machine(s):
- **Local only**: `python3 scripts/probe.py --pretty`
- **Local + Remote SSH hosts**: `python3 scripts/probe.py --hosts local,<ssh_alias1>,<ssh_alias2> --pretty`
- **Auto-discover SSH hosts from `~/.ssh/config`**: `python3 scripts/probe.py --discover-ssh`

Always examine all **five** resource dimensions before choosing where and how to run:
1. **CPU Cores & Load Average**: `idle_cores ≈ max(0, total_cores - max(load1, cpu_pct * total_cores / 100))`
2. **Real Memory vs. Reclaimable File Cache**:
   - Linux/macOS cache recently read files in RAM. **Never** look at `free` memory alone; look at `avail_gb` (available memory) and `anon_gb` (irreclaimable program memory).
   - Check `swap_free_gb`: if Swap is tiny (e.g. `<= 4 GB`), exceeding physical RAM causes an **instant hard OOM kill**.
   - Check Linux cgroup v2 slice limits (`slice.active` / `slice.max_gb`): if the user runs inside a systemd slice (like `vec.slice`), the cgroup limit (`memory.high` / `memory.max`) is the real ceiling, even if the host has 250 GB RAM.
3. **Disk Free Space & Mounts (`/`, `/tmp`, `/data`, `/alldata`)**:
   - Check where large intermediate files (`.h5ad`, `.rds`, `.parquet`, checkpoints, `/tmp` sort buffers) will be written.
   - Abort or warn if the target mount has `< 15 GB` free or `< 5%` free space.
4. **GPU (Optional / Bonus)**:
   - Many bioinformatics tasks (Scanpy, Seurat, `ot.emd`, `scipy.optimize.linear_sum_assignment`, differential expression) are **100% CPU & RAM bound**. Do not require a GPU unless the script uses PyTorch/JAX/CuPy/RAPIDS.
   - On multi-GPU servers, check per-card `util_pct` and `vram_free_gb`, filter out small display adapters (e.g. 4 GB GTX 1050 Ti), and explicitly bind `CUDA_VISIBLE_DEVICES=<idle_card_index>`.

---

### Rule 2: The 40%–60% Idle Sweet-Spot Allocation Formula
Never run single-core serial `for` loops on a multi-core machine, and never allocate 100% of the machine. Target **40%–60% of currently idle resources**:

1. **Estimate per-worker peak memory ($M_{\text{worker}}$)**:
   - Rule of thumb for tabular / single-cell / spatial data: loading a file of size $S_{\text{disk}}$ (compressed `.h5ad` / `.rds`) expands to **$3\times$ to $5\times$ in RAM** during preprocessing, distance matrix computation, or copying.
   - Always keep a host safety margin $M_{\text{margin}} = \max(8\text{ GB}, 15\% \times M_{\text{total}})$ (or $10\text{ GB}$ below `slice.high_gb`).
2. **Compute safe worker count ($N_{\text{workers}}$)**:
   $$N_{\text{cpu}} = \max\left(1, \lfloor 0.50 \times \text{Cores}_{\text{idle}} \rfloor\right)$$
   $$N_{\text{mem}} = \max\left(1, \left\lfloor \frac{M_{\text{avail\_effective}} - M_{\text{margin}}}{M_{\text{worker}}} \right\rfloor\right)$$
   $$N_{\text{workers}} = \min(N_{\text{cpu}}, N_{\text{mem}})$$
3. **Cap threads per worker ($T_{\text{threads}}$) to prevent BLAS thread explosion**:
   - When $N_{\text{workers}} > 1$ (e.g. `ProcessPoolExecutor`, `joblib`, `multiprocessing`, R `future::multisession`), **every worker** tries to spawn $N_{\text{cores}}$ OpenMP/OpenBLAS/MKL threads by default.
   - Always set:
     $$T_{\text{threads}} = \begin{cases} 1 \text{ or } 2 & \text{if } N_{\text{workers}} \ge 4 \\ \min(4, \lfloor \text{Cores}_{\text{idle}} / N_{\text{workers}} \rfloor) & \text{if } 1 < N_{\text{workers}} < 4 \\ \min(8, \lfloor 0.5 \times \text{Cores}_{\text{idle}} \rfloor) & \text{if } N_{\text{workers}} = 1 \end{cases}$$
   - Export all thread-cap variables before starting:
     ```bash
     export OMP_NUM_THREADS=$T_threads
     export OPENBLAS_NUM_THREADS=$T_threads
     export MKL_NUM_THREADS=$T_threads
     export VECLIB_MAXIMUM_THREADS=$T_threads
     export NUMEXPR_NUM_THREADS=$T_threads
     export RAYON_NUM_THREADS=$T_threads
     ```

---

### Rule 3: Always Launch Heavy Jobs with `safe_run.sh`
Use `scripts/safe_run.sh` to wrap long or memory-intensive tasks:
```bash
scripts/safe_run.sh -m 24G -w 8 -t 2 -l logs/exp1.log -- python3 train_or_eval.py
```
What `safe_run.sh` guarantees:
- **Pre-flight admission check**: Verifies free disk space (`>= 5 GB`) and waits if available memory (or systemd slice headroom) is below requested `-m` + safety margin.
- **Linux CGroup containment**: If `systemd-run --user` is available, isolates the job in a user slice (`compute.slice` or `vec.slice`) with `MemoryMax` and `CPUWeight=20` so a runaway memory leak kills *only* that script and never crashes `sshd` or other users' jobs.
- **macOS responsiveness protection**: Runs under `nice -n 10` (and `taskpolicy -b` when appropriate) with a background memory watchdog so the user's Mac never freezes while browsing papers or editing slides.
- **Automatic thread-cap injection**: Sets `OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, `MKL_NUM_THREADS`, `VECLIB_MAXIMUM_THREADS`, and `NUMEXPR_NUM_THREADS`.

---

### Rule 4: Monitor & Diagnose Stuck or Failed Jobs (`task_watch.py`)
When a user asks *"Is my job still running?"* or *"Why is it taking forever?"*, run `python3 scripts/task_watch.py` (or `--host <ssh_host>`). It automatically classifies every job into clear health states:
- 🟢 **`HEALTHY (Multi-Core)`**: Actively computing across multiple cores (`CPU% >= 150%`), memory within safe bounds.
- 🟡 **`SINGLE-CORE BOTTLENECK`**: Running at `~95–105% CPU` on a 20+ core machine. Usually means a pure-C++/Python serial loop (`ot.emd`, `linear_sum_assignment`, `for` over folds/genes) that should be parallelized with `ProcessPoolExecutor`.
- 🟠 **`THREAD THRASHING`**: Process count or thread count >> physical cores with high system/kernel wait. Caused by forgetting `OMP_NUM_THREADS=1` inside multiprocessing workers.
- 🔴 **`STALLED / HUNG (0% CPU)`**: Process exists, `elapsed > 60s`, but `CPU% < 1.0%` and stdout log has not grown. Common causes: deadlocked `multiprocessing.Queue` / PyTorch `DataLoader(num_workers>0)`, waiting on interactive stdin prompt, or hung network download.
- 💀 **`OOM KILLED (Exit 137 / SIGKILL)`**: Detected via `cgroup memory.events (oom_kill)` increment or exit status `137`. Reduce `-w` (workers), switch to memory-mapped mode (`sc.read_h5ad(..., backed='r')`), or move to a higher-RAM host.

---

## Building or Customizing a Live Monitor UI

If a user wants to build their own visual monitor for their laptop + lab servers, see [references/build_your_own_monitor.md](references/build_your_own_monitor.md):
1. **Instant Cross-Platform Web Dashboard** (`scripts/dashboard_server.py`): Run one command on Mac, Linux, or Windows to open a live dark-mode dashboard at `http://127.0.0.1:8765` showing all local & SSH hosts side by side.
2. **Native macOS Menu Bar App Blueprint**: Architecture pattern for a SwiftUI `MenuBarExtra` app that keeps a single zero-overhead streaming SSH connection (`ssh <host> "python3 -u -" < probe.py --stream`) per server, renders dual-segment memory bars (`Anon+Kernel` vs `File Cache`), per-card GPU bars, and sends native macOS notifications when jobs finish.
3. **Plain-Language Guide for Medical / Biology Researchers**: See [references/medical_student_guide.md](references/medical_student_guide.md) to explain CPU cores, Load Average, RAM vs. Cache, Swap, Disk I/O, and GPU VRAM in intuitive lab analogies.
