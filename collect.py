#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""作品 3 · 真机取证与确定性诊断 —— collect.py

**这一层的定位**：把"运维老师傅一眼就看出来的东西"变成**程序能算、能复现、能进 CI 的规则**。
LLM 只负责在规则之上做归因与措辞 —— **该由程序拿的确定性，一步都不让**（Day 34 的结论用在运维知识上）。

═══════════════════════════════════════════════════════════════════
三条工程底线
═══════════════════════════════════════════════════════════════════

**① 只读**：不写任何文件、不执行 shell、不改系统状态。
   （可选的白名单命令默认关闭；能用 /proc、/sys、Python 标准库读到的，就不去起进程。）

**② 缺数据就说缺，绝不假装有**：平台没有 /proc（比如 Windows）→ 该项标 `available: false`，
   规则**不在缺数据上触发**。这一条比"多采几项"重要得多 ——
   运维工具最危险的失败是"**没看到就报健康**"（Day 38 那条"读不到 ≠ 没有异常"的同族）。

**③ 采集本身要有上限**：读文件有字节上限、目录遍历有深度与条目上限、每项失败都不许中断整体。
   取证工具把自己卡死，是最尴尬的故障。

数据来源（Linux 优先，跨平台降级）：
    /proc/meminfo  /proc/loadavg  /proc/uptime  /proc/vmstat（oom_kill）
    /proc/sys/fs/file-nr  /proc/net/tcp（TIME_WAIT）  /proc/pressure/*（PSI 压力）
    /sys/class/thermal/thermal_zone*/temp（板子上很关键）
    /sys/fs/cgroup/memory.max|current（容器限额）  /.dockerenv
    跨平台：shutil.disk_usage（各挂载点）/ ctypes 取内存（Windows）/ os.statvfs（inode，Linux）
"""
import os
import re
import json
import time
import shutil
import platform
import subprocess

MAX_READ_BYTES = 256 * 1024          # 单文件读取上限
WALK_MAX_ENTRIES = 20000             # 目录遍历条目上限
WALK_MAX_DEPTH = 3
DEFAULT_BIG_FILE_MB = 1024           # 单个大文件告警阈值


# ══════════════════════════════════════════════════════════════════
# 底层：容错读取（只读、有上限、失败返回 None 而不是抛）
# ══════════════════════════════════════════════════════════════════
def _read(path, limit=MAX_READ_BYTES):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except (OSError, PermissionError):
        return None


def _read_int(path):
    txt = _read(path, 256)
    if txt is None:
        return None
    try:
        return int(txt.strip())
    except ValueError:
        return None


def _parse_kv(text):
    """解析 `Key: value unit` 形式的伪文件（/proc/meminfo、cgroup 等）"""
    out = {}
    for line in (text or "").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def _kb(text_from_kv, key):
    v = text_from_kv.get(key)
    if not v:
        return None
    m = re.match(r"(\d+)", v)
    return int(m.group(1)) if m else None


# ══════════════════════════════════════════════════════════════════
# 各采集项
# ══════════════════════════════════════════════════════════════════
def collect_disks(mounts=None):
    """磁盘：**每个挂载点**分别看（真实故障往往只在某一个挂载点上），Linux 还能看 inode。

    为什么 inode 必须看：磁盘"还剩 20%"但 inode 用满 100% 是经典故障 ——
    `df -h` 看着正常，程序却写不进文件。只有 `df -i` / statvfs 才看得见。
    """
    items = []
    for m in (mounts or _default_mounts()):
        entry = {"mount": m, "available": False}
        try:
            usage = shutil.disk_usage(m)
            entry.update({
                "available": True,
                "total_gb": round(usage.total / 2 ** 30, 1),
                "used_gb": round(usage.used / 2 ** 30, 1),
                "free_gb": round(usage.free / 2 ** 30, 1),
                "used_pct": round(usage.used / usage.total * 100, 1),
            })
        except (OSError, PermissionError, ValueError):
            pass
        if hasattr(os, "statvfs"):
            try:
                st = os.statvfs(m)
                total_i, free_i = st.f_files, st.f_ffree
                if total_i:
                    entry.update({"inodes_total": total_i, "inodes_free": free_i,
                                  "inode_used_pct": round((total_i - free_i) / total_i * 100, 1)})
            except (OSError, PermissionError):
                pass
        items.append(entry)
    return {"available": any(i["available"] for i in items), "items": items}


def _default_mounts():
    """Linux 看主要挂载点（从 /proc/mounts 取真实挂载）；Windows 看所有盘符。"""
    mounts = []
    txt = _read("/proc/mounts", 4096 * 8)
    if txt:
        skip = {"proc", "sysfs", "tmpfs", "devtmpfs", "cgroup", "cgroup2", "overlay",
                "devpts", "securityfs", "debugfs", "tracefs", "pstore", "bpf", "autofs",
                "mqueue", "hugetlbfs", "configfs", "fusectl", "binfmt_misc", "nsfs"}
        for line in txt.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[2] not in skip and parts[1].startswith("/"):
                if parts[1] not in mounts:
                    mounts.append(parts[1])
    if not mounts:
        if os.name == "nt":
            import string
            for d in string.ascii_uppercase:
                p = f"{d}:\\"
                if os.path.exists(p):
                    mounts.append(p)
        else:
            mounts = ["/"]
    return mounts[:12]                      # 上限：极端环境挂载点可能上百个


def collect_memory(proc_root="/proc"):
    """/proc/meminfo（Linux）或 ctypes（Windows）。**MemAvailable 才是可用内存**，
    不是 MemFree —— 后者不含可回收的 page cache，会严重低估、误报。"""
    kv = _parse_kv(_read(os.path.join(proc_root, "meminfo")))
    if kv:
        total = _kb(kv, "MemTotal")
        avail = _kb(kv, "MemAvailable") or _kb(kv, "MemFree")
        swap_total, swap_free = _kb(kv, "SwapTotal"), _kb(kv, "SwapFree")
        if total:
            out = {"available": True, "source": "proc/meminfo",
                   "total_mb": round(total / 1024, 1),
                   "available_mb": round((avail or 0) / 1024, 1),
                   "available_pct": round((avail or 0) / total * 100, 1)}
            if swap_total:
                out.update({"swap_total_mb": round(swap_total / 1024, 1),
                            "swap_used_pct": round((swap_total - (swap_free or 0)) / swap_total * 100, 1)})
            return out
    if os.name == "nt":
        try:
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = MS()
            m.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return {"available": True, "source": "win32/GlobalMemoryStatusEx",
                    "total_mb": round(m.ullTotalPhys / 2 ** 20, 1),
                    "available_mb": round(m.ullAvailPhys / 2 ** 20, 1),
                    "available_pct": round(m.ullAvailPhys / m.ullTotalPhys * 100, 1),
                    "commit_total_mb": round(m.ullTotalPageFile / 2 ** 20, 1),
                    "commit_avail_mb": round(m.ullAvailPageFile / 2 ** 20, 1)}
        except Exception:
            pass
    return {"available": False, "reason": "既没有 /proc/meminfo 也不在 Windows"}


def collect_load(proc_root="/proc"):
    """Linux 负载。**负载要跟核数一起看**：load=8 在 4 核上是过载，在 32 核上是轻闲。"""
    ncpu = os.cpu_count() or 1
    txt = _read(os.path.join(proc_root, "loadavg"), 256)
    if txt:
        try:
            parts = txt.split()
            return {"available": True, "source": "proc/loadavg", "ncpu": ncpu,
                    "load1": float(parts[0]), "load5": float(parts[1]), "load15": float(parts[2]),
                    "load1_per_cpu": round(float(parts[0]) / ncpu, 2)}
        except (ValueError, IndexError):
            pass
    return {"available": False, "reason": "非 Linux（无 /proc/loadavg）", "ncpu": ncpu}


def collect_uptime(proc_root="/proc"):
    txt = _read(os.path.join(proc_root, "uptime"), 128)
    if txt:
        try:
            return {"available": True, "seconds": float(txt.split()[0])}
        except (ValueError, IndexError):
            pass
    if os.name == "nt":
        try:
            import ctypes
            ms = ctypes.windll.kernel32.GetTickCount64()
            return {"available": True, "seconds": ms / 1000.0}
        except Exception:
            pass
    return {"available": False}


def collect_kernel(proc_root="/proc"):
    """内核侧信号：OOM kill 计数（`/proc/vmstat`）、fd 用量、TIME_WAIT、PSI 压力、cgroup 限额。

    这几项是"**真机上才有的证据**" —— 日志里往往只看到后果（进程被杀、连接被拒），
    原因在这些计数器里。板子上尤其看 PSI 与 cgroup。
    """
    out = {}

    # OOM kill 次数（累计值；>0 就说明这台机器杀过进程）
    vm = _parse_kv(_read(os.path.join(proc_root, "vmstat")))
    if vm:
        kills = vm.get("oom_kill")
        out["oom_kill"] = {"available": kills is not None,
                           "count": int(kills) if (kills or "").isdigit() else None}

    # 文件描述符：已分配 / 上限
    nr = _read(os.path.join(proc_root, "sys/fs/file-nr"), 128)
    fmax = _read_int(os.path.join(proc_root, "sys/fs/file-max"))
    if nr and fmax:
        try:
            allocated = int(nr.split()[0])
            out["fds"] = {"available": True, "allocated": allocated, "max": fmax,
                          "used_pct": round(allocated / fmax * 100, 1)}
        except (ValueError, IndexError):
            out["fds"] = {"available": False}

    # TCP TIME_WAIT 堆积（连接被拒/端口耗尽的前兆）
    tcp = _read(os.path.join(proc_root, "net/tcp"), 2 * 1024 * 1024)
    if tcp:
        tw = sum(1 for line in tcp.splitlines()[1:] if re.search(r"\s0A\s", line))
        out["tcp_time_wait"] = {"available": True, "count": tw}

    # PSI（Pressure Stall Information）：内核 4.20+，直接告诉你"有多少时间在等资源"
    psi = {}
    for res in ("cpu", "memory", "io"):
        t = _read(os.path.join(proc_root, "pressure", res), 512)
        if t:
            m = re.search(r"some avg10=([\d.]+) avg60=([\d.]+)", t)
            if m:
                psi[res] = {"avg10": float(m.group(1)), "avg60": float(m.group(2))}
    if psi:
        out["psi"] = {"available": True, "by_resource": psi}

    # cgroup 内存限额（容器里最容易被忽略的一条：主机内存充足，容器却被 OOM kill）
    limit = _read_int("/sys/fs/cgroup/memory.max") or _read_int("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    cur = _read_int("/sys/fs/cgroup/memory.current") or _read_int("/sys/fs/cgroup/memory/memory.usage_in_bytes")
    if limit and cur and limit > 0 and limit < 2 ** 60:
        out["cgroup_mem"] = {"available": True, "limit_mb": round(limit / 2 ** 20, 1),
                             "current_mb": round(cur / 2 ** 20, 1),
                             "used_pct": round(cur / limit * 100, 1)}
    return out


def collect_thermal(base="/sys/class/thermal"):
    """温度：**嵌入式/边缘设备上这是头等指标**（RK3568 这类板子过热会降频/重启）。"""
    temps = []
    if os.path.isdir(base):
        try:
            for zone in sorted(os.listdir(base)):
                if not zone.startswith("thermal_zone"):
                    continue
                val = _read_int(os.path.join(base, zone, "temp"))
                typ = (_read(os.path.join(base, zone, "type")) or "").strip()
                if val is None:
                    continue
                c = val / 1000.0 if val > 1000 else float(val)
                if -50 < c < 200:
                    temps.append({"zone": zone, "type": typ or "?", "celsius": round(c, 1)})
        except (OSError, PermissionError):
            pass
    return {"available": bool(temps), "zones": temps,
            "max_celsius": max((t["celsius"] for t in temps), default=None)}


def collect_environment():
    """环境事实：容器/虚拟机/内核版本 —— 影响诊断方向（容器里看不到宿主机的真实内存）。"""
    env = {"available": True, "platform": platform.platform(),
           "python": platform.python_version(), "ncpu": os.cpu_count() or 1,
           "kernel": platform.release(),
           "in_container": os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")}
    if os.path.exists("/sys/fs/cgroup"):
        try:
            if "docker" in (_read("/proc/1/cgroup", 4096) or ""):
                env["in_container"] = True
        except Exception:
            pass
    return env


def top_files_by_size(root, top_n=5, min_mb=1, max_entries=WALK_MAX_ENTRIES,
                      max_depth=WALK_MAX_DEPTH):
    """找出目录里最大的几个文件 —— 真实需求："**是哪个日志把盘吃满的**"。

    三条上限（缺一个都可能把自己卡死）：条目上限、深度上限、权限失败静默跳过。
    """
    found, scanned = [], 0
    root = os.path.abspath(root)
    base_depth = root.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
        if dirpath.count(os.sep) - base_depth >= max_depth:
            dirnames[:] = []
        for fn in filenames:
            scanned += 1
            if scanned > max_entries:
                break
            fp = os.path.join(dirpath, fn)
            try:
                sz = os.path.getsize(fp)
            except OSError:
                continue
            if sz >= min_mb * 2 ** 20:
                found.append({"path": fp, "mb": round(sz / 2 ** 20, 1)})
        if scanned > max_entries:
            break
    found.sort(key=lambda x: -x["mb"])
    return {"available": True, "scanned": scanned, "truncated": scanned > max_entries,
            "top": found[:top_n]}


# ══════════════════════════════════════════════════════════════════
# 快照 = 一次完整取证
# ══════════════════════════════════════════════════════════════════
def snapshot(log_dirs=None, proc_root="/proc"):
    t0 = time.time()
    snap = {
        "collected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "collect_seconds": None,
        "host": platform.node(),
        "environment": collect_environment(),
        "disks": collect_disks(),
        "memory": collect_memory(proc_root),
        "load": collect_load(proc_root),
        "uptime": collect_uptime(proc_root),
        "kernel": collect_kernel(proc_root),
        "thermal": collect_thermal(),
    }
    if log_dirs:
        snap["log_dirs"] = {d: top_files_by_size(d) for d in log_dirs}
    snap["collect_seconds"] = round(time.time() - t0, 2)
    return snap


# ══════════════════════════════════════════════════════════════════
# 规则引擎：**纯函数**（无 IO、无模型）→ 可以离线单测、可以进 CI
# ══════════════════════════════════════════════════════════════════
THRESHOLDS = {
    "disk_warn_pct": 85, "disk_crit_pct": 95,
    "inode_warn_pct": 80, "inode_crit_pct": 90,
    "mem_warn_avail_pct": 15, "mem_crit_avail_pct": 8,
    "swap_warn_used_pct": 50,
    "load_warn_x_ncpu": 1.0, "load_crit_x_ncpu": 2.0,
    "psi_mem_warn": 10, "psi_mem_crit": 20,
    "fd_warn_pct": 70, "fd_crit_pct": 85,
    "thermal_warn_c": 75, "thermal_crit_c": 85,
    "cgroup_warn_pct": 80, "cgroup_crit_pct": 90,
    "time_wait_warn": 5000,
    "big_file_warn_mb": DEFAULT_BIG_FILE_MB,
}


def _f(v):
    return f"{v:g}" if isinstance(v, (int, float)) else str(v)


def evaluate(snap, thresholds=None):
    """把快照变成**确定性发现**。规则只在 `available: true` 的数据上触发 —— 缺数据不算证据。"""
    th = {**THRESHOLDS, **(thresholds or {})}
    found = []

    def add(rule, severity, subject, evidence, suggestion):
        found.append({"rule": rule, "severity": severity, "subject": subject,
                      "evidence": evidence, "suggestion": suggestion})

    # ── 磁盘（逐挂载点）─────────────────────────────────
    for d in snap.get("disks", {}).get("items", []):
        if not d.get("available"):
            continue
        pct = d.get("used_pct")
        if pct is not None and pct >= th["disk_crit_pct"]:
            add("DISK_USED_HIGH", "critical", d["mount"],
                f"已用 {_f(pct)}%（剩余 {_f(d.get('free_gb'))} GB）",
                "立即清理/扩容；先查该挂载点上最大的文件与已删除但被占用的句柄")
        elif pct is not None and pct >= th["disk_warn_pct"]:
            add("DISK_USED_HIGH", "warning", d["mount"],
                f"已用 {_f(pct)}%（剩余 {_f(d.get('free_gb'))} GB）",
                "按趋势判断到达时间；配置阈值告警与日志轮转")
        ipct = d.get("inode_used_pct")
        if ipct is not None and ipct >= th["inode_crit_pct"]:
            add("DISK_INODE_HIGH", "critical", d["mount"],
                f"inode 已用 {_f(ipct)}%（这是 `df -h` 看不出来的那类故障）",
                "找小文件多的目录（如会话/缓存/邮件队列），清理或换文件系统设计")
        elif ipct is not None and ipct >= th["inode_warn_pct"]:
            add("DISK_INODE_HIGH", "warning", d["mount"], f"inode 已用 {_f(ipct)}%",
                "检查小文件增长速度")

    # ── 内存 ────────────────────────────────────────────
    mem = snap.get("memory", {})
    if mem.get("available"):
        apct = mem.get("available_pct")
        if apct is not None and apct <= th["mem_crit_avail_pct"]:
            add("MEM_AVAILABLE_LOW", "critical", mem.get("source", "memory"),
                f"可用内存仅 {_f(apct)}%（{_f(mem.get('available_mb'))} MB / {_f(mem.get('total_mb'))} MB）",
                "找内存大户并限制；确认是否已在换页（性能会断崖式下降）")
        elif apct is not None and apct <= th["mem_warn_avail_pct"]:
            add("MEM_AVAILABLE_LOW", "warning", mem.get("source", "memory"),
                f"可用内存 {_f(apct)}%", "观察趋势，检查缓存/泄漏增长")
        sw = mem.get("swap_used_pct")
        if sw is not None and sw >= th["swap_warn_used_pct"]:
            add("SWAP_PRESSURE", "warning", "swap",
                f"swap 已用 {_f(sw)}% —— 说明物理内存已经不够，性能会明显劣化",
                "确认业务峰值内存；考虑扩容或降低常驻集")

    # ── 负载 ────────────────────────────────────────────
    load = snap.get("load", {})
    if load.get("available"):
        x = load.get("load1_per_cpu") or 0
        if x >= th["load_crit_x_ncpu"]:
            add("LOAD_HIGH", "critical", f"load1/ncpu={_f(x)}",
                f"load1={_f(load.get('load1'))}，核数={load.get('ncpu')}",
                "定位 CPU 密集进程；确认是否在 IO 等待（结合 PSI io）")
        elif x >= th["load_warn_x_ncpu"]:
            add("LOAD_HIGH", "warning", f"load1/ncpu={_f(x)}",
                f"load1={_f(load.get('load1'))}，核数={load.get('ncpu')}", "观察是否持续")

    # ── 内核侧 ──────────────────────────────────────────
    k = snap.get("kernel", {})
    oom = k.get("oom_kill", {})
    if oom.get("available") and (oom.get("count") or 0) > 0:
        add("OOM_KILL_EVENTS", "critical", "kernel",
            f"vmstat 记录 oom_kill={oom['count']} 次 —— **这是程序被杀的真正原因**",
            "查 dmesg 里被杀进程名与内存水位；给关键进程设内存上限与重启策略")
    fd = k.get("fds", {})
    if fd.get("available"):
        p = fd.get("used_pct")
        if p is not None and p >= th["fd_crit_pct"]:
            add("FD_USAGE_HIGH", "critical", "file descriptors",
                f"已分配 {_f(p)}%（{fd.get('allocated')} / {fd.get('max')}）",
                "查泄漏句柄的进程（lsof -p）；提高 ulimit 只是权宜")
        elif p is not None and p >= th["fd_warn_pct"]:
            add("FD_USAGE_HIGH", "warning", "file descriptors", f"已分配 {_f(p)}%", "观察增长")
    tw = k.get("tcp_time_wait", {})
    if tw.get("available") and (tw.get("count") or 0) >= th["time_wait_warn"]:
        add("TCP_TIME_WAIT_HIGH", "warning", "net/tcp",
            f"TIME_WAIT 连接 {tw['count']} 条 —— 短连接过多或对端不关连接",
            "考虑连接复用（keep-alive / 连接池），别急着调内核参数")
    psi = k.get("psi", {})
    if psi.get("available"):
        for res, v in psi["by_resource"].items():
            if res == "memory" and v["avg60"] >= th["psi_mem_crit"]:
                add("PSI_MEM_PRESSURE", "critical", "PSI memory",
                    f"avg60={_f(v['avg60'])}% —— 60 秒里有这么长时间在**等内存**",
                    "内存压力已经影响业务；扩容或限制大户")
            elif res == "memory" and v["avg60"] >= th["psi_mem_warn"]:
                add("PSI_MEM_PRESSURE", "warning", "PSI memory", f"avg60={_f(v['avg60'])}%",
                    "观察趋势；这是内存不足的早期信号（比 free 更灵敏）")
    cg = k.get("cgroup_mem", {})
    if cg.get("available"):
        p = cg.get("used_pct")
        if p is not None and p >= th["cgroup_crit_pct"]:
            add("CGROUP_MEM_NEAR_LIMIT", "critical", "cgroup memory",
                f"已用 {_f(p)}%（{_f(cg.get('current_mb'))} / {_f(cg.get('limit_mb'))} MB）"
                " —— 主机内存充足但容器会先被 OOM kill",
                "提高容器限额或修内存泄漏；注意 JVM/运行时也要按 cgroup 感知限额")
        elif p is not None and p >= th["cgroup_warn_pct"]:
            add("CGROUP_MEM_NEAR_LIMIT", "warning", "cgroup memory", f"已用 {_f(p)}%", "观察")

    # ── 温度（嵌入式头等指标）──────────────────────────
    th_ = snap.get("thermal", {})
    if th_.get("available"):
        mx = th_.get("max_celsius")
        hot = max(th_["zones"], key=lambda z: z["celsius"])
        if mx is not None and mx >= th["thermal_crit_c"]:
            add("THERMAL_HIGH", "critical", hot.get("type", "thermal"),
                f"最高 {_f(mx)}°C（{hot.get('zone')}）—— 会触发降频/重启",
                "查散热与风道；确认 CPU 频率是否已被限制（降频会表现为'变慢'）")
        elif mx is not None and mx >= th["thermal_warn_c"]:
            add("THERMAL_HIGH", "warning", hot.get("type", "thermal"), f"最高 {_f(mx)}°C",
                "观察峰值负载下的温度")

    # ── 大文件（谁把盘吃满）────────────────────────────
    for d, info in (snap.get("log_dirs") or {}).items():
        for f in info.get("top", []):
            if f["mb"] >= th["big_file_warn_mb"]:
                add("BIG_FILE", "warning", f["path"],
                    f"{_f(f['mb'])} MB 的单个文件（目录 {d}）",
                    "配置轮转/压缩或归档；确认是否需要保留这么久")

    order = {"critical": 0, "warning": 1, "info": 2}
    found.sort(key=lambda x: order.get(x["severity"], 9))
    return found


def render(snap, findings):
    """把快照与发现渲染成**给人看也给模型看**的文本（数字齐全，便于核对）。"""
    L = [f"【主机体检】{snap.get('host')} @ {snap.get('collected_at')}"
         f"（采集耗时 {snap.get('collect_seconds')}s）",
         f"环境：{snap.get('environment', {}).get('platform')}"
         f"｜核数 {snap.get('environment', {}).get('ncpu')}"
         f"｜容器 {snap.get('environment', {}).get('in_container')}"]
    up = snap.get("uptime", {})
    if up.get("available"):
        L.append(f"运行时长：{up['seconds'] / 86400:.1f} 天")
    for d in snap.get("disks", {}).get("items", []):
        if d.get("available"):
            # 用 .get() 兜底：快照可能是旧版本、或人工构造的 —— 渲染层不能因为缺字段就崩
            # （实测踩到：构造的快照只给了 used_pct，渲染当场 KeyError）
            used = d.get("used_pct", "?")
            free = d.get("free_gb", "?")
            ino = f"，inode {d['inode_used_pct']}%" if d.get("inode_used_pct") is not None else ""
            L.append(f"磁盘 {d.get('mount', '?')}：已用 {used}%（剩 {free} GB）{ino}")
        else:
            L.append(f"磁盘 {d.get('mount', '?')}：**未采集到**")
    for key, label in (("memory", "内存"), ("load", "负载"), ("thermal", "温度")):
        s = snap.get(key, {})
        if s.get("available"):
            L.append(f"{label}：{json.dumps({k: v for k, v in s.items() if k != 'available'}, ensure_ascii=False)}")
        else:
            L.append(f"{label}：**未采集到**（{s.get('reason', '平台不支持')}）")
    k = snap.get("kernel", {})
    for key in ("oom_kill", "fds", "tcp_time_wait", "psi", "cgroup_mem"):
        s = k.get(key)
        if s and s.get("available"):
            L.append(f"内核·{key}：{json.dumps({kk: vv for kk, vv in s.items() if kk != 'available'}, ensure_ascii=False)}")
    L.append(f"\n【确定性发现】共 {len(findings)} 条"
             f"（critical {sum(1 for f in findings if f['severity'] == 'critical')}）")
    for f in findings:
        L.append(f"  [{f['severity']}] {f['rule']} · {f['subject']}：{f['evidence']}"
                 f"\n      建议：{f['suggestion']}")
    if not findings:
        L.append("  （规则未发现异常 —— 注意：这只代表**已采集到的维度**正常）")
    return "\n".join(L)


def save(snap, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)
    return path


def load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="真机体检（只读）")
    ap.add_argument("--log-dir", action="append", default=None,
                    help="顺便扫大文件的目录（可多次），如 --log-dir /var/log")
    ap.add_argument("--save", default=None, help="把快照存成 JSON（便于换机器分析/存档）")
    ap.add_argument("--from", dest="from_file", default=None, help="不采集，直接分析已有快照")
    args = ap.parse_args()

    s = load(args.from_file) if args.from_file else snapshot(log_dirs=args.log_dir)
    if args.save and not args.from_file:
        print(f"快照已保存：{save(s, args.save)}")
    print(render(s, evaluate(s)))
