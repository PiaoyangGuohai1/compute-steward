# ⚡ Compute Steward (`compute-steward`)

**面向医学生、生信/计算生物学研究者与 AI 编程助手的零依赖全平台算力管家 Skill + 实时看板套件。**

在跑单细胞转录组（Scanpy / Seurat）、空间组学、全基因组关联分析、最优传输（Optimal Transport）或深度学习任务时，很多非计算机科班同学和默认配置的 AI 经常遇到三大灾难：
1. **一把将服务器跑死被同组通报**：开 32 个进程 × 每个进程底层默认开 32 个 OpenBLAS 线程 = **1024 个线程互相踩踏**，整机卡死连 SSH 都登不上；
2. **狂挂任务半夜静默 OOM 被杀还不知道**：看 `.h5ad` / `.rds` 文件才 4 GB，不知道读进内存会膨胀 3–5 倍，也不知道怎么区分“程序真内存 (`Anon`)”和“可回收文件缓存 (`Cache`)”，触发小 Swap 或 CGroup 强杀（`Exit 137`）；
3. **任务卡死或单核慢跑傻等一整天**：进程还在 `top` 里，但要么因为 `DataLoader` 死锁处于 `0% CPU` 假死状态，要么忘了并行只用 1 个核在硬算。

`compute-steward` 将复杂的系统运维浓缩成 **1 个全 AI 平台通用的 `SKILL.md` + 4 个纯 Python 标准库/Shell 零依赖脚本**（无需 `pip install`，无需远端服务器 `root` 权限，支持本机 Mac / Linux / Windows + 任意远程 SSH 服务器）。

---

## 🚀 一键安装到所有 AI 工具（Claude Code / Cursor / Codex / Antigravity）

```bash
git clone https://github.com/PiaoyangGuohai1/compute-steward.git
cd compute-steward
chmod +x install.sh
./install.sh
```

`./install.sh` 会自动将 Skill 注册到：
- `~/.agents/skills/compute-steward`（通用 Agent Skills 标准目录）
- `~/.claude/skills/compute-steward`（Claude Code）
- `~/.codex/skills/compute-steward`（OpenAI Codex CLI）
- `~/.gemini/config/skills/compute-steward`（Google Antigravity / Gemini CLI）
- `~/.cursor/skills/compute-steward`（Cursor）

安装后，你只需要用自然语言对 AI 说：
- *“帮我看看我本机 Mac 和远程服务器现在有哪些空余的 CPU 和内存？我这个单细胞任务大概每个进程要 6G 内存，应该在哪台跑、开多少并行？”*
- *“帮我用安全模式提交这个脚本，防止内存爆掉或者把服务器卡死。”*
- *“帮我检查一下我刚才挂上去的任务还在正常跑吗？有没有卡住或者 OOM？”*
- *“根据 `compute-steward` 帮我启动一个监控本机和实验室服务器的实时看板。”*

---

## 🛠️ 四大核心工具（0 第三方依赖，单文件即插即用）

### 1. `scripts/probe.py` — 全维度算力体检与 40%–60% 甜点并行计算器
不局限于 GPU，全面扫描 **CPU 核数与空闲核数、内存（严格拆分不可回收的 `Anon` 程序内存与可回收 `File Cache`）、Swap 大小、Linux `cgroup` 沙箱限额、多挂载点硬盘剩余空间（`/`、`/data`、`/alldata`、`/tmp`）、多卡 GPU 逐卡显存归属（我的任务 vs 他人任务）**：

```bash
# 查看本机（Mac / Linux / Windows）资源体检与 AI 并行建议
python3 scripts/probe.py --pretty

# 同时穿透检查本机 + 多台远程 SSH 服务器（远端无需安装任何文件！）
python3 scripts/probe.py --hosts local,gpu-node1,lab-server --pretty --task-mem-gb 6
```

自动遵循 **“40%–60% 空闲资源甜点法则”**：
- 绝不只用 1 个核串行慢跑，也绝不一上来占满 100% 挤死其他同学；
- 自动根据**当前空闲核数的 50%** 与**扣除安全余量后的真实可用内存**取最小值，输出推荐的 `ProcessPoolExecutor / joblib` 并发进程数与 `OMP_NUM_THREADS` 线程锁死参数。

### 2. `scripts/safe_run.sh` — 防炸机、防 OOM、防线程爆炸安全启动器
```bash
scripts/safe_run.sh -m 24G -w 8 -t 2 -l logs/train.log -- python3 my_analysis.py
```
- **自动锁死底层数学库线程**：自动注入 `OMP_NUM_THREADS`、`OPENBLAS_NUM_THREADS`、`MKL_NUM_THREADS`、`VECLIB_MAXIMUM_THREADS`、`NUMEXPR_NUM_THREADS`，彻底杜绝多进程 × 多线程爆炸；
- **Linux 服务器 `systemd` CGroup 沙箱隔离**：自动进入 `vec.slice` 或 `compute.slice` 并限制 `MemoryMax=24G`、`CPUWeight=20`。如果内存不够会**自动排队等待**；万一代码内存泄漏，只会优雅终止当前脚本并明确报告 `💀 EXIT 137 (OOM KILLED)`，绝不会把整台服务器的 `sshd` 搞崩；
- **本地 Mac 防卡顿**：自动使用 `nice -n 10` 降级调度优先级，保证一边跑满多核计算，一边看文献、写论文、做 PPT 丝滑不掉帧。

### 3. `scripts/task_watch.py` — 在跑任务健康巡检与死锁/瓶颈诊断器
```bash
python3 scripts/task_watch.py --hosts local,gpu-node1
```
自动给每个正在运行的 Python / R / Julia / 生信任务贴上直观的诊断标签：
- 🟢 **`HEALTHY (Active Compute)`**：多核高速计算中，内存水位健康；
- 🟡 **`SINGLE-CORE BOTTLENECK`**：32 核机器上长期卡在 `~100% CPU`（只用了 1 个核），提示改用多进程并行；
- 🟠 **`THREAD / WORKER THRASHING`**：进程/线程数远超物理核数，发生线程踩踏；
- 🔴 **`STALLED / HUNG (0% CPU)`**：进程存活超过 60 秒但 `CPU% < 1%` 且无 GPU 活动，提示排查 `DataLoader` 死锁或网络卡死；
- 💀 **`NEAR OOM / PAST OOM ALERT`**：内存逼近上限或检测到 CGroup `oom_kill` 历史计数增加。

### 4. `scripts/dashboard_server.py` — 跨平台零依赖实时网页看板
一行命令在本地启动多机同屏实时监控面板（底层采用单条 SSH 流式长连接推送，点开页面 **0 毫秒等待**）：
```bash
python3 scripts/dashboard_server.py --hosts local,gpu-node1,lab-server --port 8765
# 或自动发现 ~/.ssh/config 中的所有服务器：
python3 scripts/dashboard_server.py --auto-ssh --port 8765
```
打开 `http://127.0.0.1:8765` 即可实时查看所有机器的 CPU、双段内存水位（程序实占 vs 缓存）、多卡 GPU 显存拆分、硬盘剩余空间、在跑任务列表以及动态并行参数推荐。

---

## 📚 科普与进阶定制文档

- 📖 **[给医学生与生信研究者的算力白话指南](references/medical_student_guide.md)**：用最通俗的实验室比喻讲清楚 CPU 核数、Load Average、为什么 `.h5ad` 读进内存会膨胀 5 倍、什么是 `Anon` 真内存 vs `Cache` 假占用、什么是 `Swap` 与 `Exit 137`。
- 🖥️ **[5 分钟定制你自己的实时算力看板（含 macOS 原生菜单栏 App 蓝图）](references/build_your_own_monitor.md)**：包含统一的 `probe.py` 流式 JSON Schema，以及让 AI 一键为你编译生成 macOS 原生 SwiftUI 顶部菜单栏监控小工具（带任务跑完系统弹窗通知）的完整 Prompt 模板。

---

## 📂 目录结构

```text
compute-steward/
├── SKILL.md                           # AI 助手核心指令（40%-60% 甜点配额法则、四步标准工作流）
├── README.md                          # 项目说明与快速上手
├── install.sh                         # 一键安装到 Claude / Cursor / Codex / Antigravity
├── scripts/
│   ├── probe.py                       # 零依赖全平台/SSH 穿透探针（支持单次体检 & 流式 NDJSON）
│   ├── safe_run.sh                    # 防 OOM、防线程爆炸、自动排队的跨平台安全启动器
│   ├── task_watch.py                  # 任务假死 (0% CPU)、单核瓶颈、OOM 巡检诊断器
│   └── dashboard_server.py            # 零依赖多机实时 Web 监控看板 (http://127.0.0.1:8765)
└── references/
    ├── medical_student_guide.md       # 给医学生看的算力避坑白话指南
    └── build_your_own_monitor.md      # 如何基于 probe.py 定制网页看板或 macOS 原生菜单栏 App
```
