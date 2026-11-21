#!/usr/bin/env bash
# ==============================================================================
# safe_run.sh — Cross-Platform Anti-OOM & Anti-Thread-Explosion Job Launcher
#
# Prevents the three classic laboratory computing failures:
#   1. BLAS Thread Explosion: Caps OMP/OPENBLAS/MKL/VECLIB/NUMEXPR threads.
#   2. System OOM Crash:
#      - On Linux with systemd: waits until the user slice (compute.slice or vec.slice)
#        has enough memory headroom, then runs inside a memory-capped cgroup scope
#        (MemoryMax) at low CPUWeight (20) so sshd and other users never crash.
#      - On macOS / non-systemd Linux: checks free RAM & disk space first and runs
#        under `nice -n 10` so desktop UI and system responsiveness stay smooth.
#   3. Silent / Invisible Execution: Logs start time, host, thread settings, and
#      exit status (explicitly flagging Exit 137 = OOM Killed!).
#
# Usage:
#   scripts/safe_run.sh [-m 24G] [-w 8] [-t 2] [-s compute.slice] [-l run.log] [--] <cmd...>
#
# Options:
#   -m <MEM>    Memory cap for this job (default: 16G; e.g. 8G, 24G, 48G)
#   -w <NUM>    Export SAFE_WORKERS=<NUM> for scripts reading worker count (default: auto)
#   -t <NUM>    Threads per process for OpenMP/OpenBLAS/MKL (default: 2)
#   -s <SLICE>  Linux systemd user slice name (default: vec.slice if present, else compute.slice)
#   -wait <SEC> Seconds to wait for memory headroom before exiting 75 (default: 1800)
#   -l <FILE>   Tee stdout/stderr to log file (creates parent directory automatically)
# ==============================================================================
set -euo pipefail

MEM="16G"
WORKERS=""
THREADS="${OMP_NUM_THREADS:-2}"
SLICE=""
WAIT_SEC=1800
LOG_FILE=""
MARGIN_GB=8

while (( $# )); do
  case "$1" in
    -m|--mem) MEM="$2"; shift 2 ;;
    -w|--workers) WORKERS="$2"; shift 2 ;;
    -t|--threads) THREADS="$2"; shift 2 ;;
    -s|--slice) SLICE="$2"; shift 2 ;;
    -wait|--wait) WAIT_SEC="$2"; shift 2 ;;
    -l|--log) LOG_FILE="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,27p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    --) shift; break ;;
    *) break ;;
  esac
done

(( $# )) || { echo "Usage: $0 [-m 24G] [-w 8] [-t 2] [-l job.log] [--] <cmd...>" >&2; exit 2; }

# Compatible with both macOS /bin/bash (Bash 3.2) and Linux Bash 4+/5+
parse_gb() {
  local val
  val="$(printf '%s' "$1" | tr '[:lower:]' '[:upper:]')"
  val="${val%B}"
  if [[ "$val" =~ ^([0-9]+(\.[0-9]+)?)G$ ]]; then
    awk "BEGIN {printf \"%d\", ${BASH_REMATCH[1]} * 1}"
  elif [[ "$val" =~ ^([0-9]+)M$ ]]; then
    awk "BEGIN {printf \"%d\", (${BASH_REMATCH[1]} + 1023) / 1024}"
  else
    echo 16
  fi
}

REQ_GB="$(parse_gb "$MEM")"
NEED_GB=$(( REQ_GB + MARGIN_GB ))

# 1. Check disk free space on current working directory (warn if < 5 GB)
CWD_FREE_GB=$(df -Pk . 2>/dev/null | awk 'NR==2 {printf "%d", $4 / 1048576}' || echo 100)
if (( CWD_FREE_GB < 5 )); then
  echo "[safe_run] ⚠️ WARNING: Current disk has only ${CWD_FREE_GB} GB free! Check intermediate output paths." >&2
fi

# 2. Export thread caps to prevent BLAS thread explosion
export OMP_NUM_THREADS="$THREADS"
export OPENBLAS_NUM_THREADS="$THREADS"
export MKL_NUM_THREADS="$THREADS"
export VECLIB_MAXIMUM_THREADS="$THREADS"
export NUMEXPR_NUM_THREADS="$THREADS"
export RAYON_NUM_THREADS="$THREADS"
export NUMBA_NUM_THREADS="$THREADS"
if [[ -n "$WORKERS" ]]; then
  export SAFE_WORKERS="$WORKERS"
fi

run_with_log_and_diagnosis() {
  local rc=0
  if [[ -n "$LOG_FILE" ]]; then
    mkdir -p "$(dirname "$LOG_FILE")"
    echo "[safe_run] Started at $(date '+%Y-%m-%d %H:%M:%S') | Host: $(hostname) | MemCap: $MEM | Threads/proc: $THREADS | Cmd: $*" | tee -a "$LOG_FILE"
    "$@" 2>&1 | tee -a "$LOG_FILE" || rc=$?
  else
    echo "[safe_run] Started at $(date '+%Y-%m-%d %H:%M:%S') | Host: $(hostname) | MemCap: $MEM | Threads/proc: $THREADS | Cmd: $*" >&2
    "$@" || rc=$?
  fi

  if (( rc == 137 )); then
    echo "[safe_run] 💀 EXIT 137 (SIGKILL / OOM KILLED): The job exceeded its memory limit ($MEM) or system RAM!" >&2
    echo "[safe_run] 💡 Fix: Reduce parallel workers (-w), increase -m if RAM is free, or use backed/chunked data loading." >&2
  elif (( rc != 0 )); then
    echo "[safe_run] ❌ Job exited with non-zero code: $rc" >&2
  else
    echo "[safe_run] ✅ Completed successfully at $(date '+%Y-%m-%d %H:%M:%S')" >&2
  fi
  return $rc
}

# 3. Platform-specific containment (Linux systemd cgroup v2 vs macOS / generic)
OS_TYPE="$(uname -s)"
if [[ "$OS_TYPE" == "Linux" ]] && command -v systemd-run >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  if [[ -z "$SLICE" ]]; then
    UID_NUM="$(id -u)"
    if [[ -d "/sys/fs/cgroup/user.slice/user-${UID_NUM}.slice/user@${UID_NUM}.service/vec.slice" ]] || systemctl --user list-units --type=slice --all 2>/dev/null | grep -q "vec.slice"; then
      SLICE="vec.slice"
    else
      SLICE="compute.slice"
    fi
  fi

  TOTAL_KB=$(awk '/MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null || echo 67108864)
  DEFAULT_HIGH_GB=$(( TOTAL_KB / 1048576 ))

  DEADLINE=$(( $(date +%s) + WAIT_SEC ))
  while :; do
    CUR_GB=0
    HIGH_GB=$DEFAULT_HIGH_GB
    while IFS='=' read -r key value; do
      # Guard against "infinity", "[not set]", or 64-bit UINT64_MAX (>16 digits)
      [[ "$value" =~ ^[0-9]{1,15}$ ]] || continue
      case "$key" in
        MemoryCurrent) CUR_GB=$(( value / 1073741824 )) ;;
        MemoryHigh)    HIGH_GB=$(( value / 1073741824 )) ;;
      esac
    done < <(systemctl --user show "$SLICE" -p MemoryCurrent -p MemoryHigh 2>/dev/null || true)

    AVAIL_KB=$(awk '/MemAvailable:/ {print $2}' /proc/meminfo 2>/dev/null || echo 67108864)
    AVAIL_GB=$(( AVAIL_KB / 1048576 ))

    if (( CUR_GB + NEED_GB <= HIGH_GB )) && (( AVAIL_GB >= REQ_GB / 2 + 4 )); then
      break
    fi
    if (( $(date +%s) >= DEADLINE )); then
      echo "[safe_run] ⏳ Timeout (${WAIT_SEC}s): Not enough memory room for $MEM (Slice in use: ${CUR_GB}G/${HIGH_GB}G, Host avail: ${AVAIL_GB}G)" >&2
      exit 75
    fi
    echo "[safe_run] ⏳ Waiting for memory room (Slice: ${CUR_GB}G/${HIGH_GB}G, Host avail: ${AVAIL_GB}G, need ${REQ_GB}G + ${MARGIN_GB}G margin)..." >&2
    sleep 15
  done

  run_with_log_and_diagnosis systemd-run --user --scope --quiet --slice="$SLICE" \
    -p MemoryMax="$MEM" -p MemorySwapMax=2G -p CPUWeight=20 \
    env OMP_NUM_THREADS="$THREADS" OPENBLAS_NUM_THREADS="$THREADS" \
        MKL_NUM_THREADS="$THREADS" NUMEXPR_NUM_THREADS="$THREADS" \
        VECLIB_MAXIMUM_THREADS="$THREADS" "$@"
else
  # macOS or Linux without user systemd session: run with nice priority so UI/system stays smooth
  run_with_log_and_diagnosis nice -n 10 "$@"
fi
