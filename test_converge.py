#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""告警收敛的离线测试 —— test_converge.py（免费、秒级、不要 key）

**为什么收敛必须有测试**：它的 bug 形态是"**把两件无关的事说成一条因果链**"。
值班的人会照着"根因"去修，修完发现没解决 —— 而且**看起来还很合理**，没人会怀疑。
所以这里的断言集中在四件事：

  ① **完整性不变量**：每条发现必须恰好出现在一个事件里（`sum(成员) == len(发现)`）。
     收敛工具最经典的失败是"顺手过滤掉了不想看的发现"—— 这是**丢告警**。
  ② **不许过度合并**：弱因果边**不得**参与归组（真实踩到：`温度→负载` + `内存→负载`
     两条弱边做传递闭包，把"内存不足"和"温度过高"并成了一条假链）。
  ③ **不许编根因**：没有强因果边 → root 必须是 None + 给候选/并列 + 明说未核验。
     单成员事件不许出现"连带 1 项""并列 1 项"这类自指胡说（也是真实踩到的）。
  ④ **图自检**：因果图成环/规则缺失/自相矛盾必须在 CI 里就挡住 —— 图错了归因就是错的。

跑法：.venv\\Scripts\\python.exe test_converge.py
"""
import os
import sys
import copy
import time
import json
import tempfile

# 直接跑这些脚本时也必须不依赖控制台代码页（Windows GBK 下 ✓/✗ 直接抛 UnicodeEncodeError）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import converge as V

HERE = os.path.dirname(os.path.abspath(__file__))
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}{('  —— ' + str(detail)) if detail else ''}")


# ══════════════════════════════════════════════════════════════════
# 造数据：完全手工构造，不依赖真实机器的状态
# ══════════════════════════════════════════════════════════════════
def F(rule, severity="warning", subject="s", evidence="", suggestion="do-something"):
    return {"rule": rule, "severity": severity, "subject": subject,
            "evidence": evidence, "suggestion": suggestion}


def snap_with(disks=None, log_dirs=None):
    return {"host": "h1", "disks": {"available": True, "items": disks or []},
            "log_dirs": log_dirs or {}}


def _synthetic_big():
    """合成一个"大规模巡检"：8 个挂载点都紧张 + 内存族 + 其它 → 用来压"收敛真能变短"。"""
    items, logs = [], {}
    for i in range(8):
        m = "/data%d" % i
        items.append({"mount": m, "available": True, "total_gb": 100.0, "free_gb": 2.0,
                      "used_pct": 98.0, "inode_used_pct": 91.0})
        logs[m] = {"available": True, "top": [{"path": m + "/big.log", "mb": 30000.0}]}
    snap = {"host": "synthetic", "collected_at": "t", "collect_seconds": 0.1,
            "disks": {"available": True, "items": items}, "log_dirs": logs,
            "memory": {"available": True, "available_pct": 3.0, "available_mb": 100,
                       "total_mb": 4000, "swap_used_pct": 90.0, "source": "/proc/meminfo"},
            "load": {"available": True, "load1_per_cpu": 3.0, "load1": 24.0, "ncpu": 8},
            "thermal": {"available": True, "max_celsius": 88.0,
                        "zones": [{"zone": "z", "type": "soc-thermal", "celsius": 88.0}]},
            "kernel": {"oom_kill": {"available": True, "count": 5},
                       "fds": {"available": True, "used_pct": 92.0, "allocated": 92000,
                               "max": 100000},
                       "tcp_time_wait": {"available": True, "count": 20000},
                       "psi": {"available": True, "by_resource": {"memory": {"avg60": 40.0}}},
                       "cgroup_mem": {"available": True, "used_pct": 99.0, "current_mb": 500,
                                      "limit_mb": 512}},
            "environment": {"platform": "synthetic", "ncpu": 8, "in_container": False},
            "uptime": {"available": True, "seconds": 100000}}
    import collect as _C
    return snap, _C.evaluate(snap)


def rules_of(events):
    return sorted(r for e in events for r in e["rules"])


def event_of(events, rule):
    for e in events:
        if rule in e["rules"]:
            return e
    return None


def find_bytes(events):
    """事件成员里的 (rule, subject) 多重集 —— 用来查"丢/重"。"""
    return sorted((m["rule"], str(m["subject"])) for e in events for m in e["members"])


def main():
    t0 = time.perf_counter()

    # ══════════════════════════════════════════════════════════════
    print("[1] 完整性不变量：一条发现都不能丢、不能重")
    # ══════════════════════════════════════════════════════════════
    fs = [F("MEM_AVAILABLE_LOW", "critical", "mem"), F("SWAP_PRESSURE", subject="swap"),
          F("OOM_KILL_EVENTS", "critical", "kernel"), F("PSI_MEM_PRESSURE", subject="PSI memory"),
          F("LOAD_HIGH", subject="load"), F("THERMAL_HIGH", "critical", "cpu-thermal"),
          F("FD_USAGE_HIGH", "critical", "file descriptors"), F("TCP_TIME_WAIT_HIGH", subject="net/tcp"),
          F("DISK_USED_HIGH", "warning", "/"), F("DISK_INODE_HIGH", "critical", "/"),
          F("BIG_FILE", subject="/var/log/app.log")]
    ev, rep = V.converge(fs)
    check("成员合计 == 发现数（不丢不重）", rep["integrity_ok"] and rep["integrity_detail"]
          or rep["integrity_ok"], rep["integrity_detail"])
    check("恰好一条发现出现在一个事件里（按 rule+subject 比多重集）",
          find_bytes(ev) == sorted((f["rule"], str(f["subject"])) for f in fs))
    check("事件数 < 发现数（确实压缩了）", rep["events"] < rep["findings"],
          f"{rep['findings']} → {rep['events']}")
    check("压缩率算得对", abs(rep["compression"] - round(len(fs) / len(ev), 2)) < 1e-9,
          rep["compression"])

    ev0, rep0 = V.converge([])
    check("空输入 → 0 事件且自检通过", ev0 == [] and rep0["integrity_ok"] and rep0["events"] == 0)
    check("空输入不炸渲染", "没有发现需要收敛" in V.render(ev0, rep0))

    # 收敛必须是**纯函数**：不能改调用方的发现（否则缓存指纹会被悄悄改坏）
    before = copy.deepcopy(fs)
    V.converge(fs)
    check("converge 不修改传入的 findings（纯函数）", fs == before)

    # 未知规则：保守自成实体，绝不与别人合并
    evu, _ = V.converge([F("SOME_NEW_RULE", "critical", "/data"),
                         F("DISK_USED_HIGH", "critical", "/data")])
    check("未知规则不自成合并（保守，宁可少合并）", len(evu) == 2,
          [e["rules"] for e in evu])

    # ══════════════════════════════════════════════════════════════
    print("[2] 实体与挂载点：目录不是挂载点（真实踩过的 bug）")
    # ══════════════════════════════════════════════════════════════
    snap = snap_with(disks=[{"mount": "/", "total_gb": 29.2}, {"mount": "/var", "total_gb": 10.0},
                            {"mount": "/var/log", "total_gb": 10.0}])
    check("最长前缀匹配取真正最长的挂载点",
          V.mount_of_path("/var/log/app/access.log", snap) == "/var/log")
    snap2 = snap_with(disks=[{"mount": "/"}, {"mount": "/var"}])
    check("回归：目录 /var/log 不得当挂载点，必须归到 /var",
          V.mount_of_path("/var/log/app/access.log", snap2) == "/var",
          V.mount_of_path("/var/log/app/access.log", snap2))

    ldir = {"log_dirs": {"/var/log": {"available": True, "top": [
        {"path": "/var/log/app.log", "mb": 2150.4}]}}}
    snap3 = {"host": "h", "disks": {"available": True,
                                    "items": [{"mount": "/", "total_gb": 29.2},
                                              {"mount": "/var", "total_gb": 10.0}]},
             "log_dirs": ldir["log_dirs"]}
    check("大文件占比算得出来（2150.4MB / 10GB = 21%）",
          V.file_share_pct("/var/log/app.log", "/var", snap3) == 21.0,
          V.file_share_pct("/var/log/app.log", "/var", snap3))
    snap4 = {"host": "h", "disks": {"available": True, "items": [{"mount": "/var"}]},
             "log_dirs": ldir["log_dirs"]}
    check("缺挂载点容量 → 占比返回 None（不编数字）",
          V.file_share_pct("/var/log/app.log", "/var", snap4) is None)
    check("缺文件记录 → 占比返回 None", V.file_share_pct("/nope.log", "/var", snap3) is None)
    check("挂载点信息缺失 → mount_of_path 返回 None（不猜）",
          V.mount_of_path("/var/log/x.log", {"disks": {"items": []}}) is None)
    check("Windows 盘符前缀也能匹配",
          V.mount_of_path("C:\\Users\\a\\x.log",
                          {"disks": {"items": [{"mount": "C:\\"}]}}) == "C:\\")
    check("同前缀不同挂载点不误判（/data 与 /database）",
          V.mount_of_path("/database/x.log",
                          {"disks": {"items": [{"mount": "/data"}]}}) is None)

    # ══════════════════════════════════════════════════════════════
    print("[3] 强因果边：允许断言根因")
    # ══════════════════════════════════════════════════════════════
    mem = [F("MEM_AVAILABLE_LOW", "critical", "mem", "可用内存仅 4%"),
           F("SWAP_PRESSURE", subject="swap"), F("PSI_MEM_PRESSURE", subject="PSI memory"),
           F("OOM_KILL_EVENTS", "critical", "kernel")]
    ev1, rep1 = V.converge(mem)
    check("内存族 4 条 → 1 个事件", len(ev1) == 1, [e["rules"] for e in ev1])
    e = ev1[0]
    check("kind = causal", e["kind"] == "causal", e["kind"])
    check("根因 = 可用内存低（强边唯一上游）", e["root"] and e["root"]["rule"] == "MEM_AVAILABLE_LOW")
    check("根因标记为已验证", e["root"]["verified"] is True)
    check("连带 3 项", len(e["cascade"]) == 3, [c["rule"] for c in e["cascade"]])
    check("每条连带都由根因解释且都是强边",
          all(c["explained_by"] and all(x["strength"] == "strong" and
                                        x["cause"] == "MEM_AVAILABLE_LOW"
                                        for x in c["explained_by"]) for c in e["cascade"]))
    check("有一条 event 级归因说明", bool(e["attribution_note"]))
    check("渲染里出现根因（已断言）", "根因（已断言）" in V.render(ev1, rep1))
    check("report 统计断言根因数", rep1["roots_asserted"] == 1)

    # OOM 单独出现：根因可能已经消失 —— 必须说出来，不能凭空造一个内存不足
    evo, _ = V.converge([F("OOM_KILL_EVENTS", "critical", "kernel")])
    check("只有 OOM 时不给根因（因为根因不在快照里）", evo[0]["root"] is None)
    check("并且明确写出「根因可能已经消失」", "根因可能已经消失" in evo[0]["vanished_root_note"],
          evo[0]["vanished_root_note"][:40])
    check("该提示进入建议里", "根因可能已经消失" in evo[0]["next_action"])

    # ══════════════════════════════════════════════════════════════
    print("[4] 弱边不得归组 —— 真实踩到的假因果链")
    # ══════════════════════════════════════════════════════════════
    tl = [F("THERMAL_HIGH", "critical", "soc-thermal", "82.3°C"), F("LOAD_HIGH", subject="load")]
    evt, rept = V.converge(tl)
    check("温度 + 负载：**不合并**（弱边只提示）", len(evt) == 2, [e["rules"] for e in evt])
    check("两者之间出现交叉引用", any(r["other_event"] == "EVT-2" or r["other_event"] == "EVT-1"
                                      for x in evt for r in x["related"]))
    check("交叉引用写明未断言", all("未断言" in r["text"] for x in evt for r in x["related"]))
    check("弱边事件不给根因", all(e["root"] is None for e in evt))

    mix = mem + tl
    evm, _ = V.converge(mix)
    # 内存族 4 条 → 1 个事件；温度 1 个；负载 1 个 → 共 3 个
    # （负载是独立实体，弱边不归组：它只是"可能被内存或温度解释"的第三方）
    check("内存 + 温度 + 负载：3 个事件（不因共享下游「负载」而合并）",
          len(evm) == 3, [e["rules"] for e in evm])
    mev = event_of(evm, "MEM_AVAILABLE_LOW")
    tev = event_of(evm, "THERMAL_HIGH")
    lev = event_of(evm, "LOAD_HIGH")
    check("内存事件与温度事件是两个不同的事件", mev["event_id"] != tev["event_id"])
    check("内存事件的连带里**没有**温度", all(c["rule"] != "THERMAL_HIGH" for c in mev["cascade"]))
    check("内存事件的并列里也没有温度",
          all(c["rule"] != "THERMAL_HIGH" for c in mev["parallel_members"]))
    check("负载事件同时引用了内存与温度（两条弱边各说各的）",
          {r["other_event"] for r in lev["related"]} == {mev["event_id"], tev["event_id"]},
          [(r["other_event"], r["cause"]) for r in lev["related"]])
    check("负载事件自己不给根因（弱边不断言）", lev["root"] is None)

    memload = [F("MEM_AVAILABLE_LOW", "critical", "mem"), F("LOAD_HIGH", subject="load")]
    evml, _ = V.converge(memload)
    check("内存 + 负载：不合并，只给交叉引用", len(evml) == 2)
    check("互相都有引用（双向）",
          len(event_of(evml, "LOAD_HIGH")["related"]) == 1 and
          len(event_of(evml, "MEM_AVAILABLE_LOW")["related"]) == 1)

    # ══════════════════════════════════════════════════════════════
    print("[5] 并列（同资源不同问题）：不许说成一条链")
    # ══════════════════════════════════════════════════════════════
    dk = [F("DISK_USED_HIGH", "critical", "/", "已用 97%"),
          F("DISK_INODE_HIGH", "critical", "/", "inode 93%")]
    ev2, rep2 = V.converge(dk)
    check("同挂载点两条 → 1 个事件（同资源只开一张单）", len(ev2) == 1)
    e2 = ev2[0]
    check("kind = parallel", e2["kind"] == "parallel", e2["kind"])
    check("无根因（空间与 inode 互不解释）", e2["root"] is None)
    check("**连带为空**（不许把并列说成连带）", e2["cascade"] == [])
    check("两条都在并列项里", len(e2["parallel_members"]) == 2)
    check("给出反例说明（大文件不吃 inode）",
          any("不吃 inode" in n for n in e2["non_causal_notes"]), e2["non_causal_notes"])
    check("建议里明确要求「分别处理」", "分别" in e2["next_action"], e2["next_action"])
    check("标题说清是「N 个独立问题」", "独立问题" in e2["title"], e2["title"])

    two = [F("DISK_USED_HIGH", "critical", "/"), F("DISK_USED_HIGH", "critical", "/data")]
    ev3, _ = V.converge(two)
    check("不同挂载点各盘满 → 2 个事件（不因同规则而合并）", len(ev3) == 2,
          [e["entities"] for e in ev3])

    ev4, _ = V.converge([F("FD_USAGE_HIGH", "critical", "file descriptors"),
                         F("TCP_TIME_WAIT_HIGH", subject="net/tcp")])
    check("fd 与 TIME_WAIT 无关 → 2 个事件", len(ev4) == 2)
    check("它们之间没有交叉引用（无因果边就不硬凑）",
          all(not x["related"] for x in ev4))

    # ══════════════════════════════════════════════════════════════
    print("[6] 单成员事件：不许自指胡说（真实踩到）")
    # ══════════════════════════════════════════════════════════════
    ev5, _ = V.converge([F("FD_USAGE_HIGH", "critical", "file descriptors", "88%",
                           "查泄漏句柄的进程")])
    e5 = ev5[0]
    check("单成员：连带为空", e5["cascade"] == [])
    check("单成员：并列也为空", e5["parallel_members"] == [])
    check("单成员：建议就是它自己的建议（不加「先处理最严重的」前缀）",
          e5["next_action"] == "查泄漏句柄的进程", e5["next_action"])
    check("单成员：标题不出现「连带/并列」",
          "连带" not in e5["title"] and "并列" not in e5["title"], e5["title"])
    txt = V.render(ev5, V.converge([F("FD_USAGE_HIGH", "critical", "file descriptors")])[1])
    check("渲染里没有「连带 1 项」这种自指", "连带 1 项" not in txt and "并列 1 项" not in txt)

    # ══════════════════════════════════════════════════════════════
    print("[7] 现状 + 预测 = 同一问题的两个时间点")
    # ══════════════════════════════════════════════════════════════
    sf = [F("DISK_USED_HIGH", "warning", "/var", "已用 92%"),
          F("DISK_FILL_FORECAST", "warning", "/var", "预计 53 分钟后到 95%")]
    ev6, _ = V.converge(sf)
    check("盘占用 + 填满预测 → 1 个事件", len(ev6) == 1)
    check("kind = state_forecast", ev6[0]["kind"] == "state_forecast", ev6[0]["kind"])
    check("标题取「现状」那条并注明含预测",
          "含趋势预测" in ev6[0]["title"] and "磁盘空间不足" in ev6[0]["title"],
          ev6[0]["title"])
    check("说明写清是现状+预测", "现状与预测" in ev6[0]["attribution_note"],
          ev6[0]["attribution_note"])
    check("状态+预测不报根因（它不是因果）", ev6[0]["root"] is None)

    ev7, _ = V.converge([F("DISK_SUDDEN_JUMP", "warning", "/var", "从 60% 跳到 92%"),
                         F("DISK_FILL_FORECAST", "warning", "/var", "预计 53 分钟后")])
    check("跳变 + 预测也归为同源，并提示先看跳变",
          ev7[0]["kind"] == "state_forecast" and "先看跳变" in ev7[0]["attribution_note"],
          ev7[0]["attribution_note"])

    # ══════════════════════════════════════════════════════════════
    print("[8] 弱边核验：把猜测变成可核验的事实")
    # ══════════════════════════════════════════════════════════════
    bf = [F("DISK_USED_HIGH", "warning", "/var", "已用 92%"),
          F("BIG_FILE", subject="/var/log/app.log", evidence="2150.4 MB")]
    ev8, _ = V.converge(bf, snap3)
    check("大文件与盘满不合并（弱边只做交叉引用）", len(ev8) == 2, [e["rules"] for e in ev8])
    ref = [r for x in ev8 for r in x["related"]]
    check("交叉引用里带**已核验占比**", any("已核验占比 21.0%" in r["text"] for r in ref),
          [r["text"][:60] for r in ref])
    ev8b, _ = V.converge(bf, snap4)      # 没有 total_gb
    refb = [r for x in ev8b for r in x["related"]]
    check("拿不到容量时明说「未核验」", any("未核验" in r["text"] for r in refb))
    check("核验失败也不阻止归组（组还是分开的）", len(ev8b) == 2)
    snap_nod = {"host": "h", "disks": {"items": []}, "log_dirs": {}}
    ev8c, _ = V.converge(bf, snap_nod)
    check("挂载点无法判定 → 不关联，并写明原因",
          not any(x["related"] for x in ev8c),
          [[r["text"][:50] for r in x["related"]] for x in ev8c])

    # ══════════════════════════════════════════════════════════════
    print("[9] 因果图自检（图错了 → 归因就是错的）")
    # ══════════════════════════════════════════════════════════════
    check("内置因果图自检通过（无环/规则齐全/无矛盾）", V.check_graph() == [],
          V.check_graph())
    check("强因果边数量记得住（3 条）",
          sum(1 for e in V.CAUSAL_EDGES if e["strength"] == "strong") == 3)
    check("每条边都有理由（不许有无理由的因果）",
          all((e.get("reason") or "").strip() for e in V.CAUSAL_EDGES))
    check("每条边的 compat 都实现了", all(e["compat"] in V.COMPAT for e in V.CAUSAL_EDGES))

    bad = copy.deepcopy(V.CAUSAL_EDGES)
    bad.append({"cause": "MEM_AVAILABLE_LOW", "effect": "MEM_AVAILABLE_LOW",
                "strength": "strong", "compat": "same_host", "reason": "自环"})
    check("自环能被自检抓到", any("自环" in p for p in V.check_graph(bad)))

    cyc = [{"cause": "LOAD_HIGH", "effect": "THERMAL_HIGH", "strength": "strong",
            "compat": "same_host", "reason": "伪"},
           {"cause": "THERMAL_HIGH", "effect": "LOAD_HIGH", "strength": "strong",
            "compat": "same_host", "reason": "伪"}]
    check("强子图成环能被自检抓到", any("成环" in p for p in V.check_graph(cyc)),
          V.check_graph(cyc))

    unk = [{"cause": "NO_SUCH_RULE", "effect": "LOAD_HIGH", "strength": "strong",
            "compat": "same_host", "reason": "x"}]
    check("规则不在实体表里能被抓到", any("不在 ENTITY_RULES" in p for p in V.check_graph(unk)))

    bads = [{"cause": "MEM_AVAILABLE_LOW", "effect": "LOAD_HIGH", "strength": "maybe",
             "compat": "same_host", "reason": "x"}]
    check("非法 strength 能被抓到", any("strength" in p for p in V.check_graph(bads)))

    badc = [{"cause": "MEM_AVAILABLE_LOW", "effect": "LOAD_HIGH", "strength": "strong",
             "compat": "telepathy", "reason": "x"}]
    check("未知 compat 能被抓到", any("compat" in p for p in V.check_graph(badc)))

    contra = [{"cause": "DISK_USED_HIGH", "effect": "DISK_INODE_HIGH", "strength": "strong",
               "compat": "same_host", "reason": "x"}]
    check("「互不解释」与强边自相矛盾能被抓到",
          any("矛盾" in p for p in V.check_graph(contra)), V.check_graph(contra))

    check("--check-graph 退出码为 0（进 CI 用）", V._main(["--check-graph"]) == 0)
    g = V.explain_graph()
    check("--explain-graph 能把图打给人看（含边/强度/理由/反例）",
          "→" in g and "必然" in g and "可解释" in g and "反例" in g and "现状+预测" in g)

    # ══════════════════════════════════════════════════════════════
    print("[10] 多机「共性嫌疑」：诚实近似，不许断言同源")
    # ══════════════════════════════════════════════════════════════
    per_host = [("h1", [F("FD_USAGE_HIGH", "critical", "fd")]),
                ("h2", [F("FD_USAGE_HIGH", "critical", "fd")]),
                ("h3", [F("FD_USAGE_HIGH", "critical", "fd")]),
                ("h4", [F("TCP_TIME_WAIT_HIGH", subject="net/tcp")])]
    cl = V.fleet_cluster(per_host, min_hosts=3)
    check("3/4 台同规则 → 1 个共性簇", len(cl) == 1 and cl[0]["rule"] == "FD_USAGE_HIGH",
          [(c["rule"], c["n_hosts"]) for c in cl])
    check("比例算得对（3/4 = 75%）", cl[0]["ratio_pct"] == 75.0, cl[0]["ratio_pct"])
    check("措辞是「嫌疑」不是「根因」", "嫌疑" in cl[0]["verdict"], cl[0]["verdict"])
    check("明说没有跨机时钟对齐", "没有跨机时钟对齐" in cl[0]["note"], cl[0]["note"][:40])
    cl2 = V.fleet_cluster(per_host, min_hosts=5)
    check("台数不够就不报（宁缺勿滥）", cl2 == [])
    cl3 = V.fleet_cluster([("h1", [F("X")]), ("h2", [F("X")]), ("h3", [F("X")]),
                           ("h4", []), ("h5", [])], min_hosts=3)
    check("分母用全部受检主机（3/5 = 60%）", cl3[0]["ratio_pct"] == 60.0, cl3[0]["ratio_pct"])
    check("多机渲染带⚠ 边界说明", "⚠" in V.render_fleet(cl, 4))
    check("没有簇时的渲染也有交代", "没有同一规则" in V.render_fleet([], 4))

    # ══════════════════════════════════════════════════════════════
    print("[11] 真实 fixture 端到端（板子 dump：14 条发现）")
    # ══════════════════════════════════════════════════════════════
    import collect as C
    dump = os.path.join(HERE, "logs", "fixtures", "linux_dump_board.txt")
    if os.path.exists(dump):
        with open(dump, "r", encoding="utf-8", errors="replace") as f:
            snap_r = C.parse_linux_dump(f.read())
        fs_r = C.evaluate(snap_r)
        evr, repr_ = V.converge(fs_r, snap_r)
        check("真实数据：完整性自检通过", repr_["integrity_ok"], repr_["integrity_detail"])
        check("真实数据：确实压缩了", repr_["events"] < repr_["findings"],
              f"{repr_['findings']} → {repr_['events']}")
        check("真实数据：不丢任何发现",
              find_bytes(evr) == sorted((f["rule"], str(f["subject"])) for f in fs_r))
        me = event_of(evr, "MEM_AVAILABLE_LOW")
        check("真实数据：内存事件断言根因 = MEM_AVAILABLE_LOW",
              me and me["root"] and me["root"]["rule"] == "MEM_AVAILABLE_LOW")
        check("真实数据：OOM 在内存事件的连带里，不在别处",
              any(c["rule"] == "OOM_KILL_EVENTS" for c in me["cascade"]))
        de = event_of(evr, "DISK_INODE_HIGH")
        check("真实数据：inode 与空间并列、不给根因", de["kind"] == "parallel"
              and de["root"] is None)
        be = event_of(evr, "BIG_FILE")
        check("真实数据：大文件与 /var 盘满互相引用（未合并）",
              len(be["related"]) >= 1 and
              any("已核验占比" in r["text"] for r in be["related"]),
              [r["text"][:50] for r in be["related"]])
        check("真实数据：渲染含收敛率与边界声明",
              "压缩" in V.render(evr, repr_) and "单机单次快照" in V.render(evr, repr_))
        check("真实数据：每个事件都有 id 且唯一",
              len({e["event_id"] for e in evr}) == len(evr))
        check("真实数据：事件按严重度排序（critical 在前）",
              [e["severity"] for e in evr] == sorted(
                  [e["severity"] for e in evr], key=lambda s: V.SEVERITY_ORDER[s]))
    else:
        check("真实 fixture 存在", False, dump)

    # ══════════════════════════════════════════════════════════════
    print("[12] 给模型的紧凑输入：收敛必须真的更短（实测踩过反向）")
    # ══════════════════════════════════════════════════════════════
    import context as X
    fsb = [F("MEM_AVAILABLE_LOW", "critical", "mem", "可用内存仅 4%", "找内存大户"),
           F("SWAP_PRESSURE", subject="swap", evidence="swap 已用 78%", suggestion="扩容"),
           F("OOM_KILL_EVENTS", "critical", "kernel", "oom_kill=3", "查 dmesg"),
           F("LOAD_HIGH", subject="load", evidence="load1=6.24", suggestion="观察"),
           F("FD_USAGE_HIGH", "critical", "fd", "88%", "lsof"),
           F("DISK_INODE_HIGH", "critical", "/", "inode 93%", "清小文件"),
           F("DISK_USED_HIGH", "critical", "/", "已用 97%", "清大文件")]
    evb, repb = V.converge(fsb)
    llm = V.render_for_llm(evb, repb)
    check("紧凑输入里带「发现总数未减少」的提醒（防模型漏报）",
          "发现总数未减少" in llm, llm.splitlines()[0])
    check("紧凑输入包含每个事件 id", all(e["event_id"] in llm for e in evb))
    check("紧凑输入标注了候选根因「不得写成结论」",
          "不得写成结论" in llm or all(not e["candidate_root"] for e in evb))

    # ── 关键不变量：收敛后的模型输入**不得比原始输入更长** ──
    # （第一版在这里是 +26.9%：重复的非因果说明 + 弱关联全文 → 收敛反而更贵，
    #   "优化"变成"增本"，所以必须在测试里钉死）
    big_snap, big_findings = _synthetic_big()
    ev_big, rep_big = V.converge(big_findings, big_snap)
    full_big = C.render(big_snap, big_findings)
    llm_big = V.render_for_llm(ev_big, rep_big, snap=big_snap)
    check("合成大场景（33 条发现）确实触发了折叠",
          any("同类" in ln for ln in llm_big.splitlines()),
          [ln for ln in llm_big.splitlines() if "同类" in ln][:1])
    check("折叠不减少事件数（事件照旧全部存在）", len(ev_big) >= 3, len(ev_big))
    check("折叠后所有 event_id 仍在输入里（一个都不能漏）",
          all(e["event_id"] in llm_big for e in ev_big),
          [e["event_id"] for e in ev_big if e["event_id"] not in llm_big])
    check("★ 大场景：收敛输入比原始输入更短",
          X.estimate_tokens(llm_big) < X.estimate_tokens(full_big),
          f"{X.estimate_tokens(full_big)} → {X.estimate_tokens(llm_big)} token")
    # 阈值取实测能守住的数（不为了让数字好看而调参）：实测约省 20~30%。
    # 这条的作用是**挡住"收敛反而更贵"的回归**（第一版是 +26.9%），不是刷指标。
    check("★ 大场景：token 至少省 15%（挡住反向回归）",
          X.estimate_tokens(llm_big) <= X.estimate_tokens(full_big) * 0.85,
          f"省 {(1 - X.estimate_tokens(llm_big) / X.estimate_tokens(full_big)) * 100:.1f}%")

    if os.path.exists(dump):
        ev_fx, rep_fx = V.converge(fs_r, snap_r)
        full_fx = C.render(snap_r, fs_r)
        llm_fx = V.render_for_llm(ev_fx, rep_fx, snap=snap_r)
        check("★ 真实 fixture：收敛输入也更短",
              X.estimate_tokens(llm_fx) < X.estimate_tokens(full_fx),
              f"{X.estimate_tokens(full_fx)} → {X.estimate_tokens(llm_fx)} token")
        check("真实 fixture：token 至少省 15%",
              X.estimate_tokens(llm_fx) <= X.estimate_tokens(full_fx) * 0.85,
              f"省 {(1 - X.estimate_tokens(llm_fx) / X.estimate_tokens(full_fx)) * 100:.1f}%")
        check("真实 fixture：所有 event_id 都在输入里",
              all(e["event_id"] in llm_fx for e in ev_fx))
        check("输入里带机器环境摘要（不丢上下文）", "环境：" in llm_fx, llm_fx.splitlines()[1])

    # ══════════════════════════════════════════════════════════════
    print("[13] 结构化输出（接监控/告警管道）")
    # ══════════════════════════════════════════════════════════════
    check("事件可 JSON 序列化（不含内部下标）", json.dumps(evr, ensure_ascii=False)
          and "_members_idx" not in json.dumps(evr, ensure_ascii=False))
    check("每个事件都有后续动作（可执行）", all(e["next_action"] for e in evr))
    check("每个事件都有实体标签（人能对上机器）", all(e["entities"] for e in evr))

    print("\n" + "=" * 74)
    print(f"结果：{len(PASS)} 项通过，{len(FAIL)} 项失败（耗时 {time.perf_counter() - t0:.2f}s）")
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
        sys.exit(1)
    print("收敛是纯函数 + 写死的因果图 —— 免费、可复现，已进 CI 免费档。")


if __name__ == "__main__":
    main()
