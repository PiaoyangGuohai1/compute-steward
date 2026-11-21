#!/usr/bin/env python3
"""
dashboard_server.py — Zero-Dependency Live Multi-Host Compute Dashboard (compute-steward).

Runs anywhere (macOS, Linux, Windows) using ONLY the Python 3 standard library.
Maintains persistent background streaming probes (`probe.py --stream`) to local and remote SSH hosts
so clicking or refreshing the browser returns instantly (0 ms network wait).

Usage:
  python3 scripts/dashboard_server.py                                     # Monitor local machine on http://127.0.0.1:8765
  python3 scripts/dashboard_server.py --hosts local,gpu-node1,lab-server  # Monitor local machine + remote SSH servers
  python3 scripts/dashboard_server.py --auto-ssh --port 8765              # Auto-discover hosts from ~/.ssh/config
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PROBE_PATH = Path(__file__).resolve().parent / "probe.py"
PROBE_BYTES = PROBE_PATH.read_bytes()

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Compute Steward — 实时算力与任务监控看板</title>
<style>
  :root {
    --bg: #0f172a; --card: #1e293b; --border: #334155; --text: #f8fafc; --muted: #94a3b8;
    --blue: #3b82f6; --green: #22c55e; --amber: #f59e0b; --red: #ef4444; --purple: #a855f7; --indigo: #6366f1;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", Roboto, sans-serif; padding: 18px 24px; }
  header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 18px; flex-wrap: wrap; gap: 12px; }
  h1 { font-size: 18px; font-weight: 700; display: flex; align-items: center; gap: 10px; }
  .badge { font-size: 11px; padding: 2px 8px; border-radius: 999px; font-weight: 600; background: #1e3a8a; color: #93c5fd; }
  .controls { display: flex; align-items: center; gap: 12px; font-size: 12px; color: var(--muted); }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); gap: 16px; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 16px; display: flex; flex-direction: column; gap: 14px; }
  .host-head { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border); padding-bottom: 10px; }
  .host-title { font-size: 15px; font-weight: 700; display: flex; align-items: center; gap: 8px; }
  .dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }
  .dot.ok { background: var(--green); box-shadow: 0 0 8px var(--green); }
  .dot.err { background: var(--red); }
  .meta { font-size: 11px; color: var(--muted); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  .section { display: flex; flex-direction: column; gap: 5px; }
  .sec-row { display: flex; justify-content: space-between; align-items: baseline; font-size: 12px; }
  .sec-title { font-weight: 600; color: #e2e8f0; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; }
  .bar-bg { width: 100%; height: 8px; background: #0f172a; border-radius: 999px; overflow: hidden; display: flex; }
  .bar-seg { height: 100%; transition: width 0.35s ease; }
  .sub-note { font-size: 11px; color: var(--muted); display: flex; justify-content: space-between; flex-wrap: wrap; gap: 4px; }
  .advice-box { background: rgba(59, 130, 246, 0.08); border: 1px solid rgba(59, 130, 246, 0.3); border-radius: 8px; padding: 10px 12px; font-size: 11.5px; display: flex; flex-direction: column; gap: 5px; }
  .advice-title { font-weight: 700; color: #60a5fa; font-size: 12px; }
  .code-line { background: #0b1120; padding: 5px 8px; border-radius: 5px; font-family: ui-monospace, SFMono-Regular, monospace; font-size: 11px; color: #cbd5e1; overflow-x: auto; white-space: nowrap; }
  .warn-box { background: rgba(239, 68, 68, 0.12); border: 1px solid rgba(239, 68, 68, 0.4); color: #fca5a5; padding: 7px 10px; border-radius: 6px; font-size: 11px; }
  .job-item { background: #0f172a; border: 1px solid #1e293b; border-radius: 7px; padding: 8px 10px; font-size: 11.5px; display: flex; flex-direction: column; gap: 4px; }
  .job-top { display: flex; justify-content: space-between; align-items: center; gap: 6px; }
  .job-cmd { font-family: ui-monospace, SFMono-Regular, monospace; font-weight: 600; color: #e2e8f0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .gpu-card-row { display: grid; grid-template-columns: 44px 42px 1fr 90px; align-items: center; gap: 6px; font-size: 11px; font-family: ui-monospace, SFMono-Regular, monospace; padding-top: 2px; }
</style>
</head>
<body>
<header>
  <h1>⚡ Compute Steward <span class="badge">40%–60% 空闲资源甜点分配器</span></h1>
  <div class="controls">
    <span>每进程预估内存: <input id="taskMem" type="number" value="4" min="0.5" max="128" step="0.5" style="width:52px;background:#1e293b;color:#fff;border:1px solid #475569;border-radius:4px;padding:2px 5px;"> GB</span>
    <span id="statusText">正在连接...</span>
  </div>
</header>
<div id="grid" class="grid"></div>

<script>
function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
function pct(v, max) { return max > 0 ? Math.min(100, Math.max(0, (v / max) * 100)).toFixed(1) : 0; }
function fmtDur(s) {
  s = Number(s) || 0;
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s/60) + "m" + (s%60) + "s";
  return Math.floor(s/3600) + "h" + Math.floor((s%3600)/60) + "m";
}

function renderHost(h) {
  if (!h.ok && h.error) {
    return `<div class="card">
      <div class="host-head">
        <div class="host-title"><span class="dot err"></span>${esc(h.alias || h.host)}</div>
        <span class="meta">离线 / 连接中</span>
      </div>
      <div class="warn-box">${esc(h.error)}</div>
    </div>`;
  }
  const s = h.data;
  const cpu = s.cpu, mem = s.mem, gpu = s.gpu, sl = s.vec_slice, disk = s.disk, rec = s.recommendation || {};
  const cpuColor = cpu.pct > 85 ? "var(--amber)" : "var(--blue)";
  const anonPct = pct(mem.anon_gb, mem.total_gb);
  const cachePct = pct(mem.cache_gb, mem.total_gb);

  let sliceHtml = "";
  if (sl && sl.active && sl.has_limit) {
    const rPct = pct(sl.real_gb, sl.max_gb);
    const fPct = pct(sl.file_gb, sl.max_gb);
    sliceHtml = `<div class="section">
      <div class="sec-row">
        <span class="sec-title">🛡️ CGroup 沙箱配额</span>
        <span class="mono">实占 <b>${sl.real_gb}G</b> / ${sl.high_gb}G High (${sl.max_gb}G Max)</span>
      </div>
      <div class="bar-bg">
        <div class="bar-seg" style="width:${rPct}%;background:var(--purple)"></div>
        <div class="bar-seg" style="width:${fPct}%;background:rgba(168,85,247,0.3)"></div>
      </div>
      <div class="sub-note">
        <span>🟣 不可回收 Anon+Kernel: ${sl.real_gb}G · 文件缓存: ${sl.file_gb}G</span>
        <span>OOM Kills: <b style="color:${sl.oom_kill>0?'var(--red)':'inherit'}">${sl.oom_kill}</b></span>
      </div>
    </div>`;
  }

  let gpuHtml = "";
  if (gpu && gpu.ok) {
    let cardsHtml = "";
    if (gpu.cards && gpu.cards.length > 0) {
      cardsHtml = gpu.cards.map(c => {
        const myP = pct(c.my_vram_gb, c.vram_total_gb);
        const othP = pct(c.other_vram_gb, c.vram_total_gb);
        return `<div class="gpu-card-row">
          <span>GPU${Number(c.index)}</span>
          <span style="color:${c.util_pct>80?'var(--amber)':'var(--green)'}">${Number(c.util_pct)}%</span>
          <div class="bar-bg" style="height:6px">
            <div class="bar-seg" style="width:${myP}%;background:var(--green)"></div>
            <div class="bar-seg" style="width:${othP}%;background:var(--amber)"></div>
          </div>
          <span style="text-align:right">剩 ${Number(c.vram_free_gb).toFixed(1)}/${Number(c.vram_total_gb).toFixed(0)}G</span>
        </div>`;
      }).join("");
    }
    const gMyP = pct(gpu.my_vram_gb, gpu.vram_total_gb || mem.total_gb);
    const gOthP = pct(gpu.other_vram_gb, gpu.vram_total_gb || mem.total_gb);
    gpuHtml = `<div class="section">
      <div class="sec-row">
        <span class="sec-title">🎮 GPU (${esc(gpu.name)})</span>
        <span class="mono">算力 <b>${gpu.util_pct}%</b> ${gpu.vram_total_gb>0 ? `· 显存剩 <b>${gpu.vram_free_gb}G</b>/${gpu.vram_total_gb}G` : ''}</span>
      </div>
      <div class="bar-bg">
        <div class="bar-seg" style="width:${Math.max(gpu.util_pct, gMyP)}%;background:var(--green)"></div>
        <div class="bar-seg" style="width:${gOthP}%;background:var(--amber)"></div>
      </div>
      ${cardsHtml}
    </div>`;
  }

  const mountsHtml = (disk.mounts || []).map(m => `${esc(m.mount)}: <b>${m.free_gb}G</b> 剩`).join(" · ");

  const jobsHtml = (s.jobs || []).length === 0
    ? `<div class="sub-note">当前无在跑计算任务</div>`
    : (s.jobs || []).map(j => `<div class="job-item">
        <div class="job-top">
          <span class="job-cmd" title="${esc(j.cmd)}">${esc(j.title)}</span>
          <span class="mono" style="color:${j.cpu_pct<1&&j.elapsed_sec>60?'var(--red)':'var(--green)'}">CPU ${j.cpu_pct}% · ${j.mem_gb}G · ${fmtDur(j.elapsed_sec)}</span>
        </div>
        <div class="sub-note">
          <span>PID ${Number(j.pid)} · ${Number(j.proc_count)} 进程 · ${j.in_vec_slice ? '🛡️ '+esc(j.slice) : '⚠️ 未进沙箱'}</span>
          ${j.vram_gb > 0 ? `<span>VRAM ${j.vram_gb}G</span>` : ''}
        </div>
      </div>`).join("");

  const warnsHtml = (rec.warnings || []).map(w => `<div class="warn-box">⚠️ ${esc(w)}</div>`).join("");

  return `<div class="card">
    <div class="host-head">
      <div class="host-title"><span class="dot ok"></span>${esc(h.alias)} <span class="meta">(${esc(s.host)} · ${esc(s.os)})</span></div>
      <span class="meta">${esc(s.link)}</span>
    </div>

    <!-- CPU -->
    <div class="section">
      <div class="sec-row">
        <span class="sec-title">⚙️ CPU (${cpu.cores} 核)</span>
        <span class="mono">利用率 <b>${cpu.pct}%</b> · 空闲约 <b>${rec.idle_cores_est} 核</b></span>
      </div>
      <div class="bar-bg"><div class="bar-seg" style="width:${cpu.pct}%;background:${cpuColor}"></div></div>
      <div class="sub-note"><span>Load (1/5/15m): ${cpu.load1} / ${cpu.load5} / ${cpu.load15}</span></div>
    </div>

    <!-- Memory -->
    <div class="section">
      <div class="sec-row">
        <span class="sec-title">🧠 内存 (RAM & Swap)</span>
        <span class="mono">真实可用 <b style="color:${mem.avail_gb<12?'var(--red)':'var(--green)'}">${mem.avail_gb}G</b> / ${mem.total_gb}G</span>
      </div>
      <div class="bar-bg">
        <div class="bar-seg" style="width:${anonPct}%;background:var(--indigo)" title="程序实占 Anon"></div>
        <div class="bar-seg" style="width:${cachePct}%;background:rgba(99,102,241,0.3)" title="可自动回收文件缓存"></div>
      </div>
      <div class="sub-note">
        <span>程序实占(Anon): ${mem.anon_gb}G · 文件缓存(可回收): ${mem.cache_gb}G</span>
        <span>Swap: ${mem.swap_used_gb}/${mem.swap_total_gb}G</span>
      </div>
    </div>

    ${sliceHtml}
    ${gpuHtml}

    <!-- Disk -->
    <div class="section">
      <div class="sec-row"><span class="sec-title">💾 硬盘剩余空间</span><span class="mono">${mountsHtml}</span></div>
    </div>

    <!-- AI Recommendation -->
    <div class="advice-box">
      <div class="advice-title">💡 AI 任务推荐配置（40%–60% 空闲甜点区）</div>
      <div>建议并行进程: <b>${rec.recommended_workers} 进程</b> × 每进程 <b>${rec.threads_per_worker} 线程</b>（内存安全上限 <b>${rec.safe_mem_budget_gb}G</b>）</div>
      <div class="code-line">${esc(rec.safe_run_cmd || '')}</div>
    </div>

    ${warnsHtml}

    <!-- Active Jobs -->
    <div class="section">
      <div class="sec-row"><span class="sec-title">🏃 我的在跑任务 (${(s.jobs||[]).length})</span></div>
      ${jobsHtml}
    </div>
  </div>`;
}

async function refresh() {
  const mem = document.getElementById("taskMem").value || 4;
  try {
    const res = await fetch(`/api/state?task_mem_gb=${encodeURIComponent(mem)}`);
    const data = await res.json();
    document.getElementById("grid").innerHTML = data.hosts.map(renderHost).join("");
    document.getElementById("statusText").textContent = "实时更新中 · " + new Date().toLocaleTimeString();
  } catch (e) {
    document.getElementById("statusText").textContent = "连接断开: " + e;
  }
}
setInterval(refresh, 2000);
refresh();
</script>
</body>
</html>
"""


class HostStreamWorker:
    """Keeps a continuous streaming probe (`probe.py --stream`) or periodic poll for one host."""

    def __init__(self, alias: str):
        self.alias = alias
        self.lock = threading.Lock()
        self.latest: dict | None = None
        self.error: str = "Initializing probe..."
        self.updated_at: float = 0.0
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        is_local = self.alias in ("local", "localhost", "mac")
        while True:
            try:
                if is_local:
                    cmd = [sys.executable, "-u", str(PROBE_PATH), "--stream", "--interval", "2.0"]
                    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                else:
                    cmd = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=15",
                           self.alias, "python3 -u - --stream --interval 2.0"]
                    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    proc.stdin.write(PROBE_BYTES)
                    proc.stdin.close()

                assert proc.stdout is not None
                for raw_line in proc.stdout:
                    line = raw_line.decode("utf-8", errors="replace").strip() if isinstance(raw_line, bytes) else raw_line.strip()
                    if not line or not line.startswith("{"):
                        continue
                    payload = json.loads(line)
                    with self.lock:
                        self.latest = payload
                        self.error = ""
                        self.updated_at = time.time()
                proc.wait()
                with self.lock:
                    self.error = f"Stream closed (code {proc.returncode}), reconnecting..."
            except Exception as e:
                with self.lock:
                    self.error = str(e)
            time.sleep(3.0)

    def snapshot(self, task_mem_gb: float = 4.0) -> dict:
        from probe import compute_recommendations
        with self.lock:
            if self.latest is None:
                return {"alias": self.alias, "ok": False, "error": self.error}
            copy_data = dict(self.latest)
            copy_data["recommendation"] = compute_recommendations(copy_data, task_mem_gb=task_mem_gb)
            return {"alias": self.alias, "ok": True, "age_sec": round(time.time() - self.updated_at, 1), "data": copy_data}


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from probe import discover_ssh_hosts

    ap = argparse.ArgumentParser(description="Live Multi-Host Web Dashboard for Compute Steward")
    ap.add_argument("--hosts", type=str, default="local", help="Comma-separated hosts (e.g. local,gpu-node1,lab-server)")
    ap.add_argument("--auto-ssh", action="store_true", help="Automatically include all hosts from ~/.ssh/config")
    ap.add_argument("--host", type=str, default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    hosts = [h.strip() for h in args.hosts.split(",") if h.strip()]
    if args.auto_ssh:
        for h in discover_ssh_hosts():
            if h not in hosts:
                hosts.append(h)

    workers = [HostStreamWorker(h) for h in hosts]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):
            return

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/api/state":
                try:
                    tmem = float((q.get("task_mem_gb") or ["4.0"])[0])
                except ValueError:
                    tmem = 4.0
                body = json.dumps({"hosts": [w.snapshot(tmem) for w in workers]}, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"⚡ Compute Steward Live Dashboard running at: http://{args.host}:{args.port}")
    print(f"   Monitoring hosts: {', '.join(hosts)}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
