#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真机取证与规则引擎的离线测试 —— test_collect.py（**不调模型、不花钱、秒级**）

为什么这一层必须有测试（而不是"跑一下看着对"）：
  规则引擎的 bug 形态是**漏报**与**误报** ——
    · 漏报：磁盘 96% 却没告警 → 真出事故时你在睡觉
    · 误报：**缺数据的平台上照样告警**（Windows 上没有 /proc，规则却按"没有负载 = 负载正常"算）
  两者都不会报错。所以阈值边界、缺数据行为、平台差异都必须用断言钉住。

覆盖：
    [1] 阈值边界：每条规则在 warn/crit 的**临界值上**必须触发，刚低于必须不触发
    [2] 缺数据不误报：available=false 的维度一条规则都不许触发
    [3] 真实快照：本机能采、结构齐、不抛异常、只读（不写任何文件）
    [4] 目录扫描有上限：大目录不超时、权限失败不中断
    [5] 渲染：包含关键数字，且**不读取任何密钥类文件**
"""
import os
import sys
import json
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collect as C

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}{('  —— ' + str(detail)) if detail else ''}")

def rules(snap, **th):
    return {f["rule"] for f in C.evaluate(snap, th or None)}


def snap(**over):
    """造一个"全维度可用但都健康"的快照，再按用例覆盖 —— 这样能精确控制变量。"""
    base = {
        "host": "test-host", "collected_at": "2026-09-24 00:00:00", "collect_seconds": 0.1,
        "environment": {"available": True, "platform": "Linux-test", "ncpu": 4, "in_container": False},
        "disks": {"available": True, "items": [
            {"mount": "/", "available": True, "used_pct": 40.0, "free_gb": 60.0, "inode_used_pct": 10.0}]},
        "memory": {"available": True, "source": "proc/meminfo", "total_mb": 8000.0,
                   "available_mb": 4000.0, "available_pct": 50.0, "swap_used_pct": 0.0},
        "load": {"available": True, "ncpu": 4, "load1": 1.0, "load1_per_cpu": 0.25},
        "uptime": {"available": True, "seconds": 86400.0},
        "kernel": {"oom_kill": {"available": True, "count": 0},
                   "fds": {"available": True, "allocated": 1000, "max": 100000, "used_pct": 1.0},
                   "tcp_time_wait": {"available": True, "count": 12},
                   "psi": {"available": True, "by_resource": {"memory": {"avg10": 0.1, "avg60": 0.2}}},
                   "cgroup_mem": {"available": True, "limit_mb": 2000.0, "current_mb": 500.0, "used_pct": 25.0}},
        "thermal": {"available": True, "zones": [{"zone": "thermal_zone0", "type": "soc", "celsius": 45.0}],
                    "max_celsius": 45.0},
    }
    for k, v in over.items():
        base[k] = v
    return base


def main():
    t0 = time.perf_counter()

    # ── [1] 阈值边界（每条规则都要在临界值上验，刚过/刚不到都测）────────
    print("[1] 阈值边界")
    K = snap()["kernel"]                      # 健康的内核维度，单点覆盖时不至于误伤
    cases = [
        ("磁盘 95% → crit", snap(disks={"available": True, "items": [
            {"mount": "/", "available": True, "used_pct": 95.0}]}), "DISK_USED_HIGH", "critical"),
        ("磁盘 94.9% → warn（还没到 95）", snap(disks={"available": True, "items": [
            {"mount": "/", "available": True, "used_pct": 94.9}]}), "DISK_USED_HIGH", "warning"),
        ("磁盘 84.9% → 不报（低于 85 阈值）", snap(disks={"available": True, "items": [
            {"mount": "/", "available": True, "used_pct": 84.9}]}), "DISK_USED_HIGH", None),
        ("磁盘 85% → warn", snap(disks={"available": True, "items": [
            {"mount": "/", "available": True, "used_pct": 85.0}]}), "DISK_USED_HIGH", "warning"),
        ("inode 90%（df -h 看不出来）→ crit", snap(disks={"available": True, "items": [
            {"mount": "/", "available": True, "used_pct": 30.0, "inode_used_pct": 90.0}]}),
         "DISK_INODE_HIGH", "critical"),
        ("内存可用 8% → crit", snap(memory={"available": True, "available_pct": 8.0, "source": "m"}),
         "MEM_AVAILABLE_LOW", "critical"),
        ("内存可用 15.1% → 不报", snap(memory={"available": True, "available_pct": 15.1, "source": "m"}),
         "MEM_AVAILABLE_LOW", None),
        ("swap 用 50% → warn", snap(memory={"available": True, "available_pct": 40.0,
                                           "swap_used_pct": 50.0, "source": "m"}),
         "SWAP_PRESSURE", "warning"),
        ("load1 = 2×核数 → crit", snap(load={"available": True, "ncpu": 4, "load1": 8.0,
                                            "load1_per_cpu": 2.0}), "LOAD_HIGH", "critical"),
        ("load1 = 0.99×核数 → 不报", snap(load={"available": True, "ncpu": 4, "load1": 3.96,
                                              "load1_per_cpu": 0.99}), "LOAD_HIGH", None),
        ("oom_kill = 1（程序被杀的真正原因）→ crit",
         snap(kernel={**K, "oom_kill": {"available": True, "count": 1}}), "OOM_KILL_EVENTS", "critical"),
        ("fd 85% → crit", snap(kernel={**K, "fds": {"available": True, "used_pct": 85.0}}),
         "FD_USAGE_HIGH", "critical"),
        ("TIME_WAIT 5000 → warn", snap(kernel={**K, "tcp_time_wait": {"available": True, "count": 5000}}),
         "TCP_TIME_WAIT_HIGH", "warning"),
        ("PSI memory avg60=20 → crit", snap(kernel={**K, "psi": {
            "available": True, "by_resource": {"memory": {"avg10": 5.0, "avg60": 20.0}}}}),
         "PSI_MEM_PRESSURE", "critical"),
        ("cgroup 90%（主机充足也会被容器 OOM）→ crit",
         snap(kernel={**K, "cgroup_mem": {"available": True, "used_pct": 90.0}}),
         "CGROUP_MEM_NEAR_LIMIT", "critical"),
        ("温度 85°C（板子降频/重启）→ crit",
         snap(thermal={"available": True, "max_celsius": 85.0,
                       "zones": [{"zone": "z0", "type": "soc", "celsius": 85.0}]}), "THERMAL_HIGH", "critical"),
        ("温度 74.9°C → 不报",
         snap(thermal={"available": True, "max_celsius": 74.9,
                       "zones": [{"zone": "z0", "type": "soc", "celsius": 74.9}]}), "THERMAL_HIGH", None),
        ("单文件 1024MB → warn", snap(log_dirs={"/var/log": {"available": True, "top": [
            {"path": "/var/log/app.log", "mb": 1024.0}]}}), "BIG_FILE", "warning"),
    ]
    for name, s_obj, rule, want_sev in cases:
        hits = [f for f in C.evaluate(s_obj) if f["rule"] == rule]
        if want_sev is None:
            check(name, not hits, f"却触发了 {[f['severity'] for f in hits]}")
        else:
            check(name, any(f["severity"] == want_sev for f in hits),
                  f"实际 {[f['severity'] for f in hits] or '无'}")

    # ── [2] 缺数据不许误报（这是运维工具最危险的失败）────────────────
    print("\n[2] 缺数据不误报")
    empty = {"host": "h", "collected_at": "t", "disks": {"available": False, "items": [
        {"mount": "/", "available": False}]},
        "memory": {"available": False, "reason": "非 Linux"},
        "load": {"available": False, "reason": "非 Linux"},
        "kernel": {}, "thermal": {"available": False}}
    check("全维度不可用时：零发现", C.evaluate(empty) == [], C.evaluate(empty))
    only_disk = snap(load={"available": False, "reason": "非 Linux"},
                     kernel={}, thermal={"available": False},
                     memory={"available": False, "reason": "非 Linux"},
                     disks={"available": True, "items": [{"mount": "/", "available": True, "used_pct": 99.0}]})
    got = rules(only_disk)
    check("只有磁盘可用时：只报磁盘，不报别的", got == {"DISK_USED_HIGH"}, sorted(got))
    check("渲染里显式写出'未采集到'", "未采集到" in C.render(only_disk, C.evaluate(only_disk)))
    check("零发现时也提示'只代表已采集维度正常'",
          "只代表" in C.render(empty, []))
    # 回归：字段不全的快照（旧版本/人工构造）不许让渲染崩 —— 实测踩到过 KeyError
    partial = {"host": "h", "collected_at": "t", "disks": {"available": True, "items": [
        {"mount": "/", "available": True, "used_pct": 91.0}]}}      # 故意不给 free_gb
    try:
        txt = C.render(partial, C.evaluate(partial))
        check("字段不全的快照渲染不崩（回归）", "91" in txt and "?" in txt)
    except Exception as e:
        check("字段不全的快照渲染不崩（回归）", False, f"{type(e).__name__}: {e}")

    # ── [3] 真实快照（本机）──────────────────────────────
    print("\n[3] 真实快照（本机，只读）")
    s = C.snapshot()
    check("能采集且结构齐", all(k in s for k in ("disks", "memory", "load", "kernel", "thermal",
                                                "environment", "uptime")))
    check("磁盘可用（跨平台一定能拿到）", s["disks"]["available"], s["disks"]["items"][:1])
    check("内存可用（Linux /proc 或 Windows ctypes）", s["memory"]["available"],
          s["memory"].get("source"))
    check("平台缺的维度标成不可用而不是编造",
          isinstance(s["load"].get("available"), bool) and isinstance(s["thermal"].get("available"), bool))
    check("采集耗时被记录", isinstance(s["collect_seconds"], (int, float)))
    f = C.evaluate(s)
    check("规则能跑通且每条都有证据与建议",
          all(x["evidence"] and x["suggestion"] for x in f), f"{len(f)} 条发现")
    # 只读性：快照前后目录内容不变（不写任何文件）
    before = set(os.listdir(os.path.dirname(os.path.abspath(__file__))))
    C.snapshot()
    after = set(os.listdir(os.path.dirname(os.path.abspath(__file__))))
    check("只读：采集不产生任何文件", before == after, sorted(after - before))

    # ── [4] 目录扫描有上限、权限失败不中断 ─────────────────
    print("\n[4] 目录扫描")
    here = os.path.dirname(os.path.abspath(__file__))
    t = time.perf_counter()
    r = C.top_files_by_size(here, top_n=3, min_mb=0, max_entries=500)
    dt = time.perf_counter() - t
    check("能扫出最大的文件且不超时", dt < 5 and isinstance(r["top"], list), f"{dt:.2f}s")
    r2 = C.top_files_by_size(os.path.abspath(os.sep), top_n=2, max_entries=200)
    check("根目录扫描受 max_entries 限制（不卡死）", r2["scanned"] <= 201, r2["scanned"])
    r3 = C.top_files_by_size(os.path.join(here, "不存在的目录"), top_n=1, max_entries=10)
    check("不存在的目录不抛异常", r3["available"] and r3["top"] == [])

    # ── [5] 渲染不泄漏密钥 ────────────────────────────────
    print("\n[5] 渲染内容")
    txt = C.render(s, f)
    check("渲染含主机名与时间", s["host"][:4] in txt and s["collected_at"] in txt)
    check("渲染不含密钥特征串（体检不该读密钥文件）",
          not any(k in txt for k in ("sk-", "DEEPSEEK_API_KEY", "SECRET", "PASSWORD")))

    # ── [6] Linux dump 解析（板子/VM 上 shell 采集 → 开发机解析）──────────
    # 这条链的意义：目标机没有 Python/pip，但有 /proc 和 busybox sh；
    # 脚本只搬事实，判断全在分析侧 —— 所以解析器必须能离线测。
    print("\n[6] Linux dump 解析（板子采集 → 开发机分析）")
    dump_path = os.path.join(here, "logs", "fixtures", "linux_dump_board.txt")
    with open(dump_path, "r", encoding="utf-8") as f:
        dump = f.read()
    ds = C.parse_linux_dump(dump)
    check("主机名/时间解析出来", ds["host"] == "rk3568-board" and ds["collected_at"].startswith("2026-09-24"))
    check("核数解析（load 要跟核数比才有意义）", ds["environment"]["ncpu"] == 4)
    check("磁盘逐挂载点解析（跳过 devtmpfs/tmpfs）",
          {d["mount"] for d in ds["disks"]["items"]} == {"/", "/var", "/boot"},
          [d["mount"] for d in ds["disks"]["items"]])
    check("挂载点用量与时 inode 合并",
          abs([d for d in ds["disks"]["items"] if d["mount"] == "/"][0]["used_pct"] - 97.0) < 0.1
          and [d for d in ds["disks"]["items"] if d["mount"] == "/"][0]["inode_used_pct"] == 93.0)
    check("内存可用率按 MemAvailable 算（不是 MemFree）",
          abs(ds["memory"]["available_pct"] - 4.4) < 0.2, ds["memory"]["available_pct"])
    check("swap 用量解析", ds["memory"]["swap_used_pct"] > 70)
    check("oom_kill 计数解析", ds["kernel"]["oom_kill"]["count"] == 3)
    check("TIME_WAIT 解析", ds["kernel"]["tcp_time_wait"]["count"] == 7213)
    check("PSI memory 解析", abs(ds["kernel"]["psi"]["by_resource"]["memory"]["avg60"] - 28.44) < 0.01)
    check("cgroup 限额解析", ds["kernel"]["cgroup_mem"]["used_pct"] > 95)
    check("温度解析（毫摄氏度 → 摄氏度）", ds["thermal"]["max_celsius"] == 82.3, ds["thermal"]["max_celsius"])
    check("CPU 频率解析（kHz → MHz）", ds["cpu_freq_mhz"]["max"] == 1800.0)
    check("大文件解析", ds["log_dirs"]["/var/log"]["top"][0]["mb"] > 2000)
    fired = {f["rule"] for f in C.evaluate(ds)}
    expect = {"DISK_USED_HIGH", "DISK_INODE_HIGH", "MEM_AVAILABLE_LOW", "SWAP_PRESSURE",
              "LOAD_HIGH", "OOM_KILL_EVENTS", "FD_USAGE_HIGH", "TCP_TIME_WAIT_HIGH",
              "PSI_MEM_PRESSURE", "CGROUP_MEM_NEAR_LIMIT", "THERMAL_HIGH", "BIG_FILE"}
    check("规则在真实 dump 上全部触发（12 条齐活）", expect <= fired, sorted(expect - fired))
    check("渲染能吃 Linux dump 的快照", "rk3568-board" in C.render(ds, C.evaluate(ds)))
    # 空/残缺 dump 不许崩、也不许编造
    empty = C.parse_linux_dump("###HOST\nlinux\n###END")
    check("残缺 dump 解析不崩且全标不可用",
          empty["disks"]["available"] is False and empty["memory"]["available"] is False
          and C.evaluate(empty) == [])

    print("\n" + "=" * 74)
    print(f"结果：{len(PASS)} 项通过，{len(FAIL)} 项失败（耗时 {time.perf_counter() - t0:.2f}s）")
    if FAIL:
        for x in FAIL:
            print(f"  ✗ {x}")
        sys.exit(1)
    print("规则引擎是纯函数 —— 所以这些测试不花钱、不需要 key，可以直接进 CI 的免费档。")


def _unused():
    """（保留占位说明：边界用例已改成直接传快照对象，读起来更直白）"""
    return s


if __name__ == "__main__":
    main()
