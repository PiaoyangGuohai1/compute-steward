# 如何用 `compute-steward` 在 5 分钟内做出你自己的实时算力监控看板

很多人羡慕实验室大佬的 Mac 顶部菜单栏或副屏上有一个能实时看到 **本机 Mac + 所有远程服务器（CPU、内存真实水位、每张 GPU 显存谁在用、硬盘还剩多少、我的任务跑完弹窗通知）** 的看板。

通过 `compute-steward`，你完全不需要在远程服务器上安装任何守护进程或获取 `root` 权限。核心奥秘只有一句话：

> **“单条 SSH 流式管道（Zero-Install Streaming Probe）”**：本地维持一条 `ssh <host> "python3 -u - --stream"` 长连接，把零依赖的 `scripts/probe.py` 从标准输入喂给远端 Python，远端直接在内存里读 `/proc`、`/sys/fs/cgroup` 和 `libnvidia-ml.so.1`，每 2 秒吐回一行标准 JSON。

---

## 方式一：零代码开箱即用 — 跨平台实时网页看板（Mac / Windows / Linux 通用）

我们已经内置了纯 Python 标准库编写的 `scripts/dashboard_server.py`。只要在你的电脑终端运行：

```bash
# 仅监控本机
python3 scripts/dashboard_server.py

# 同时监控本机 + 你在 ~/.ssh/config 里配好的远程服务器
python3 scripts/dashboard_server.py --hosts local,gpu-node1,lab-cluster

# 或者自动扫描 ~/.ssh/config 里的所有服务器
python3 scripts/dashboard_server.py --auto-ssh --port 8765
```

打开浏览器访问 `http://127.0.0.1:8765`，就能看到：
- 每台机器的 **CPU 利用率与空闲核数**；
- **双段式内存进度条**：严格区分 **程序实占 (`Anon`)** 与 **可回收文件缓存 (`Cache`)**，以及 Linux `cgroup` 沙箱红线；
- **多卡 GPU 逐卡拆解**：自动过滤亮机卡，逐张显示 `GPU0..N` 的算力%、**我的显存 (绿)** vs **同机其他人显存 (橙)**；
- **AI 甜点配置建议**：在顶部输入你的单进程预估内存（例如 `6 GB`），所有机器卡片实时算出推荐的 `-w` 并行进程数和启动命令。

---

## 方式二：让 AI 帮你一键生成 macOS 原生菜单栏 App（SwiftUI `MenuBarExtra` 蓝图）

如果你想做一个常驻在 Mac 顶部状态栏的原生 Swift 应用（显示 `GPU:12% CPU:18% · 44G [2]`，点开是毛玻璃多机面板，任务跑完自动弹 macOS 系统通知），只需要把下面的 Prompt 发给你的 AI（Claude Code / Cursor / Antigravity）：

### 给 AI 的一键生成 Prompt 模板

```markdown
请读取 `compute-steward` skill 中的 `scripts/probe.py` 输出 JSON Schema，帮我用 SwiftUI (`MenuBarExtra(.window)`) 写一个单文件原生 macOS 菜单栏监控应用 `ComputeMonitor.swift` 和 `build.sh`：

1. **数据源协议**：
   - 对我的主机列表（例如 `["local", "gpu-node1", "lab-cluster"]`）：
     - `"local"` 直接启动子进程 `/usr/bin/python3 -u probe.py --stream --interval 2.0`
     - 远程主机启动 `/usr/bin/ssh -T -o BatchMode=yes -o ServerAliveInterval=15 <alias> "python3 -u - --stream --interval 2.0"`，将 bundle 内的 `probe.py` 写入 `stdin` 并关闭 `stdin`。
   - 按行读取 `stdout` 的 NDJSON，解码为 Swift `Codable` 结构体。断线后每 3 秒自动重连。
2. **UI 布局**：
   - 顶部标签栏支持「多机同屏（横向并排或上下折叠）」与单机切换。
   - 内存条采用双段进度条：第一段显示不可回收的 `anon_gb`（超过 85% 变红），第二段半透明显示可回收的 `cache_gb`。
   - 如果 `gpu.cards` 数组长度 > 1，逐行渲染每张显卡的 `util_pct`、我的显存 vs 他人显存双段条、剩余显存与温度。
   - 底部列出 `jobs` 数组（我的在跑任务），当某个节点的 `jobs.count` 从 `>0` 变为 `0`（且任务运行时长 >= 6 秒）时，调用 `UNUserNotificationCenter` 发送 macOS 系统通知提示“实验已完成”。
3. **编译脚本**：
   - 使用 `swiftc -O -parse-as-library -target arm64-apple-macosx14.0` 编译并打包为 `ComputeMonitor.app`，把 `probe.py` 放入 `Contents/Resources/probe.py`。
```

---

## 附：`probe.py` 标准输出 JSON Schema（供自定义前端 / 看板对接）

无论是做网页、Mac 菜单栏、Windows 托盘还是 VS Code 插件，`probe.py` 吐出的单行 JSON 格式完全统一：

```json
{
  "ok": true,
  "ts": 1795238431.9,
  "host": "gpu-node01",
  "os": "linux",
  "link": "LAN (192.168.1.50)",
  "uptime_sec": 3448539,
  "cpu": { "cores": 48, "pct": 18.9, "load1": 8.59, "load5": 7.43, "load15": 7.23 },
  "mem": {
    "total_gb": 251.6, "avail_gb": 89.6, "used_gb": 162.0,
    "anon_gb": 122.4, "cache_gb": 55.1, "swap_used_gb": 2.0, "swap_total_gb": 2.0
  },
  "gpu": {
    "ok": true, "kind": "nvidia_dedicated", "name": "4× NVIDIA L40", "count": 4,
    "util_pct": 76, "temp_c": 73, "power_w": 979.6,
    "vram_total_gb": 180.0, "vram_used_gb": 86.2, "vram_free_gb": 93.8,
    "my_vram_gb": 0.0, "other_vram_gb": 83.2,
    "cards": [
      { "index": 1, "name": "NVIDIA L40", "util_pct": 4, "temp_c": 42, "power_w": 84.2,
        "vram_total_gb": 45.0, "vram_used_gb": 37.7, "vram_free_gb": 7.3, "my_vram_gb": 0.0, "other_vram_gb": 36.9 }
    ]
  },
  "vec_slice": {
    "active": true, "has_limit": true, "current_gb": 12.4, "real_gb": 10.1,
    "anon_gb": 9.8, "file_gb": 2.3, "kernel_gb": 0.3, "swap_gb": 0.0,
    "high_gb": 56.0, "max_gb": 64.0, "oom_kill": 0, "high_throttle": 0
  },
  "disk": {
    "free_gb": 895.0, "total_gb": 3665.0,
    "data_mount": "/data", "data_free_gb": 1781.0, "data_total_gb": 11089.0,
    "mounts": [
      { "mount": "/", "label": "root", "free_gb": 895.0, "total_gb": 3665.0, "used_pct": 75.6 }
    ]
  },
  "jobs": [
    {
      "id": "run-r123.scope", "pid": 41820, "title": "train_spatial_ot.py --fold 1",
      "cmd": "python3 -u train_spatial_ot.py --fold 1", "slice": "compute", "in_vec_slice": true,
      "cpu_pct": 580.4, "mem_gb": 14.25, "mem_max_gb": 24.0, "vram_gb": 0.0,
      "elapsed_sec": 342, "proc_count": 6
    }
  ],
  "recommendation": {
    "idle_cores_est": 39.0,
    "effective_avail_mem_gb": 45.9,
    "safe_mem_budget_gb": 39.9,
    "recommended_workers": 9,
    "threads_per_worker": 2,
    "recommended_cuda_device": 1,
    "gpu_advice": "GPU 1 (NVIDIA L40) is idle (4% util, 7.3G free)",
    "env_exports": "OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 MKL_NUM_THREADS=2 NUMEXPR_NUM_THREADS=2 CUDA_VISIBLE_DEVICES=1",
    "safe_run_cmd": "scripts/safe_run.sh -m 39G -w 9 -t 2 -- <your_command>",
    "warnings": []
  }
}
```
