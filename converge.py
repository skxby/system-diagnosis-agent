#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""作品 3 · 告警收敛与根源/连带归因 —— converge.py

**为什么要有这个模块**（不是"功能清单上好看"，是企业调研里排第一的痛点）：

    一次网络抖动 → 几十台机器报警；一块盘满了 → 6 条告警。
    运维真正要处理的只有一两个，剩下全是**同一个故障的连带表现**。
    告警泛滥的代价不是"看着烦"，是**真信号被噪声埋掉**（狼来了）。

本模块把"规则发现（finding）"收敛成"事件（event）"：

    18 条发现  →  4 个事件      ← 一个故障一条事件，根因一条，连带挂在下面

═══════════════════════════════════════════════════════════════════
四条不让步的设计（每条都对应一个"看起来更聪明但更坏"的做法）
═══════════════════════════════════════════════════════════════════
**① 因果由代码判定，不由模型判定。**
    LLM 擅长措辞，不擅长"这两件事是不是一个因果链"。把它交给模型 = 不可复现、不可审计、
    而且它会**自信地编出因果**。所以因果边在这里是**写死的领域知识表**（`CAUSAL_EDGES`），
    每条都带 `strength` 和文字理由，`--explain-graph` 可完整打印给人审 —— **人可以推翻我们的图**，
    这比"AI 说的"更值得信任。

**② 分强弱，弱因果不许断言根因。**
    `strong`：机制上近乎必然（内存耗尽 → OOM killer）。
    `plausible`：**能解释，但不是唯一解释**（日志目录里有个大文件 → 盘满；也可能盘是别的目录吃满的）。
    `plausible` 只用来**归组和给候选**，绝不写成"根因是 X"。宁可输出「未定」，
    也不给一个自信的错误根因 —— 误判根因会让人去修错的东西，比不收敛更糟。

**③ 收敛不能吃掉告警（完整性不变量）。**
    每条 finding 必须**恰好**出现在一个事件里：`sum(len(事件成员)) == len(findings)`。
    这个不变量写进了测试与运行时自检 —— 收敛工具最常见的失败就是"顺手过滤掉了不想看的发现"。
    （**抑制误报是另一件事**，走反馈闭环，见 P1-2；收敛只归组，不删除。）

**④ 不许把"空间相邻/时间相邻"当因果。**
    最典型：同一挂载点的「空间满」与「inode 满」**互不解释** ——
    大文件不吃 inode，海量小文件不占多少空间，是两个独立配额、要分别处理。
    这种**反例**专门写在 `NON_CAUSAL_NOTES` 里并进测试，防止"聚类看着挺像就并起来"。

═══════════════════════════════════════════════════════════════════
诚实边界（我们不假装做过的事）
═══════════════════════════════════════════════════════════════════
· **本模块收敛的是「单机单次快照」内的发现**。企业文案里的"几十台机器报警"是**跨机时序聚类**，
  它需要中心侧带时间戳的告警流（同一时间窗 + 拓扑 + 变更事件）。我们没接那个数据源，
  所以**不做**，只在 `fleet_cluster()` 里给一个诚实的近似：**同一批巡检窗口内、同一规则在多台机器触发
  → 标记为「共性嫌疑」**，并明说它证明不了同一时刻。
· **根因可能已经不在快照里**。OOM 杀掉进程后内存就恢复了 —— 快照里只剩 `OOM_KILL_EVENTS`，
  看不到 `MEM_AVAILABLE_LOW`。所以收敛**不要求根因当前可见**，事件会写
  「根因已消失但连带仍在（典型：OOM 杀进程后内存恢复）」。
· 因果边表是**可修改的**（`--edges file.json` 覆盖），因为不同系统（板子/云主机/数据库）
  的因果不一样；但**改完要能自证无环**（`check_graph()`，进 CI）。
"""
import os
import sys
import json
import argparse

# ══════════════════════════════════════════════════════════════════
# ① 规则 → 受影响实体
# ══════════════════════════════════════════════════════════════════
# 为什么按**规则**映射而不是解析 subject 文本：subject 是人写的展示文案
# （`available memory` / `SoC 温度` / `MemAvailable`），拿它当键会漏合并。
# 映射表还顺便说明了"这条规则到底在说哪个资源"。
ENTITY_RULES = {
    # 磁盘：一个挂载点是一个实体（同一挂载点的空间/inode/预测/跳变都算同一个资源）
    "DISK_USED_HIGH": ("mount", "subject"),
    "DISK_INODE_HIGH": ("mount", "subject"),
    "DISK_FILL_FORECAST": ("mount", "subject"),
    "INODE_FILL_FORECAST": ("mount", "subject"),
    "DISK_SUDDEN_JUMP": ("mount", "subject"),
    "BIG_FILE": ("file", "subject"),          # 单文件 → 文件实体，挂载点另外解析
    # 内存：主机内存与 cgroup 内存是**两个作用域**，刻意不合并
    # （主机内存充足但容器先被 OOM kill 是常见且重要的区分）
    "MEM_AVAILABLE_LOW": ("host", "memory"),
    "MEM_DEGRADING": ("host", "memory"),
    "SWAP_PRESSURE": ("host", "memory"),
    "PSI_MEM_PRESSURE": ("host", "memory"),
    "CGROUP_MEM_NEAR_LIMIT": ("cgroup", "memory"),
    # 其它主机级资源
    "OOM_KILL_EVENTS": ("host", "kernel"),
    "LOAD_HIGH": ("host", "cpu"),
    "FD_USAGE_HIGH": ("host", "fd"),
    "TCP_TIME_WAIT_HIGH": ("host", "net"),
    "THERMAL_HIGH": ("host", "thermal"),
    "THERMAL_RISING": ("host", "thermal"),
}

ENTITY_LABELS = {
    ("host", "memory"): "主机内存",
    ("cgroup", "memory"): "容器内存（cgroup）",
    ("host", "kernel"): "内核",
    ("host", "cpu"): "CPU/负载",
    ("host", "fd"): "文件描述符",
    ("host", "net"): "网络",
    ("host", "thermal"): "温度",
}

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def mount_of_path(path, snap=None):
    """把文件路径归到挂载点。**归不到就返回 None，不猜。**

    只用**真实挂载点**（`disks.items[].mount`）做最长前缀匹配。
    为什么不能用 `log_dirs` 的目录当候选（实测踩到的真 bug）：
    `/var/log/app/access.log` 用「目录」当候选时会匹配到更长的 `/var/log`，
    于是它和挂载点 `/var` 的盘满发现**永远关联不上** —— 关联静默失效，还不报错。
    所以：**目录不是挂载点，别混进候选**。挂载点拿不到就返回 None，
    宁可说「无法判断该文件属于哪个挂载点」，也不把空间相邻当因果。
    """
    p = _norm_path(path)
    if not p:
        return None
    best, best_norm = None, None
    for d in (snap or {}).get("disks", {}).get("items", []) or []:
        raw = d.get("mount")
        if not raw:
            continue
        nm = _norm_path(raw)
        if p == nm or p.startswith(nm.rstrip("/") + "/"):
            if best_norm is None or len(nm) > len(best_norm):
                best, best_norm = raw, nm      # 返回**原始**挂载点字符串（Windows 的下游要靠它对齐）
    return best


def file_share_pct(path, mount, snap=None):
    """大文件占该挂载点**容量**的比例（%）。**拿不到就返回 None。**

    这一条把「大文件是盘满的候选来源」从**猜测**变成**可核验的事实**：
    文件大小和文件系统总量都是采集到的硬数据，比例算得出来。
    但注意它的**语义上限**：占比大只说明它是「用量的重要组成」，不等于它就是"原因" ——
    所以它只用来把措辞从「未核验」升级为「已核验占比 X%」，**不用来断言根因**。
    """
    info = None
    for _d, di in ((snap or {}).get("log_dirs") or {}).items():
        for f in (di or {}).get("top", []) or []:
            if f.get("path") == path:
                info = f
                break
        if info:
            break
    if not info or info.get("mb") is None:
        return None
    mts = _norm_path(mount)
    total_gb = None
    for d in (snap or {}).get("disks", {}).get("items", []) or []:
        if _norm_path(d.get("mount")) == mts:
            total_gb = d.get("total_gb")
            break
    if not total_gb:
        return None
    return round(info["mb"] / (total_gb * 1024) * 100, 1)


def _norm_path(p):
    if not p:
        return ""
    return p.replace("\\", "/").rstrip("/") or "/"


# ══════════════════════════════════════════════════════════════════
# ② 因果边表（写死的领域知识；每条都能被人审计和推翻）
# ══════════════════════════════════════════════════════════════════
# strength：
#   strong    —— 机制上近乎必然，允许据此断言"根因"
#   plausible —— 能解释，但不是唯一解释，只给"候选根因"，明说未核验
CAUSAL_EDGES = [
    # ── 内存族：这是唯一一条能干净断言根因的链 ──
    {"cause": "MEM_AVAILABLE_LOW", "effect": "SWAP_PRESSURE", "strength": "strong",
     "compat": "same_host",
     "reason": "物理内存不足时内核才会把页换出到 swap：swap 被用起来是内存紧张的结果"},
    {"cause": "MEM_AVAILABLE_LOW", "effect": "PSI_MEM_PRESSURE", "strength": "strong",
     "compat": "same_host",
     "reason": "可用内存低 → 任务在 60 秒窗口里长时间等内存（PSI 就是「等」的度量）"},
    {"cause": "MEM_AVAILABLE_LOW", "effect": "OOM_KILL_EVENTS", "strength": "strong",
     "compat": "same_host",
     "reason": "内存耗尽才会触发 OOM killer；反过来 OOM 后内存会恢复，所以根因可能已从快照里消失"},
    # ── 只算"能解释"的弱边 ──
    {"cause": "MEM_AVAILABLE_LOW", "effect": "LOAD_HIGH", "strength": "plausible",
     "compat": "same_host",
     "reason": "换页/等内存会让可运行队列变长；但 load 高也可能与内存完全无关"},
    {"cause": "CGROUP_MEM_NEAR_LIMIT", "effect": "OOM_KILL_EVENTS", "strength": "plausible",
     "compat": "same_host",
     "reason": "容器限额触顶会被 cgroup 杀；但 vmstat 的 oom_kill 不区分主机级还是 cgroup 级，无法据此断言"},
    {"cause": "CGROUP_MEM_NEAR_LIMIT", "effect": "PSI_MEM_PRESSURE", "strength": "plausible",
     "compat": "same_host",
     "reason": "cgroup 触顶会让组内任务等内存；主机侧 PSI 可能来自别的 cgroup"},
    {"cause": "THERMAL_HIGH", "effect": "LOAD_HIGH", "strength": "plausible",
     "compat": "same_host",
     "reason": "降频后任务完成变慢，load 会上升；但 load 高也可能与温度无关"},
    {"cause": "BIG_FILE", "effect": "DISK_USED_HIGH", "strength": "plausible",
     "compat": "file_on_mount",
     "reason": "该大文件就落在这个挂载点上，是「盘满」的候选来源 —— "
               "它是用量的重要组成，但**不等于是原因**（别处也可能吃满）"},
    {"cause": "BIG_FILE", "effect": "DISK_FILL_FORECAST", "strength": "plausible",
     "compat": "file_on_mount",
     "reason": "该文件若在增长，就是磁盘增长预测的直接来源（占比由下面核验）"},
]

# 同一实体、但**互不解释**的组合：必须显式说明，否则运维会以为"一条链修完就好"
NON_CAUSAL_NOTES = {
    frozenset({"DISK_USED_HIGH", "DISK_INODE_HIGH"}):
        "同一挂载点的「空间」与「inode」是两套独立配额：大文件不吃 inode，海量小文件不占多少空间。"
        "**这两条互不解释，要分别处理**（合并显示只是为了同一个挂载点只开一张单）",
    frozenset({"DISK_INODE_HIGH", "BIG_FILE"}):
        "大文件**不会**导致 inode 耗尽（方向相反）；inode 满通常来自海量小文件",
    frozenset({"FD_USAGE_HIGH", "LOAD_HIGH"}):
        "fd 接近上限与负载高没有必然因果（fd 泄漏往往在负载不高时也能发生）",
}

# 同一问题的「现状 + 预测」：不是因果，是同一件事的两个时间点，应合成一条
SAME_PROBLEM_PAIRS = {
    frozenset({"DISK_USED_HIGH", "DISK_FILL_FORECAST"}): "磁盘占用：现状与预测",
    frozenset({"DISK_INODE_HIGH", "INODE_FILL_FORECAST"}): "inode 占用：现状与预测",
    frozenset({"MEM_AVAILABLE_LOW", "MEM_DEGRADING"}): "可用内存：现状与预测",
    frozenset({"THERMAL_HIGH", "THERMAL_RISING"}): "温度：现状与预测",
    frozenset({"DISK_SUDDEN_JUMP", "DISK_FILL_FORECAST"}):
        "磁盘跳变与线性外推：跳变会让外推失真，**先看跳变**",
    frozenset({"DISK_SUDDEN_JUMP", "DISK_USED_HIGH"}):
        "磁盘跳变与占用高：同一次猛写的两个视角",
}

TITLES = {
    "MEM_AVAILABLE_LOW": "内存不足",
    "MEM_DEGRADING": "可用内存持续下降（预测）",
    "SWAP_PRESSURE": "swap 被大量使用",
    "PSI_MEM_PRESSURE": "内存压力（任务在等内存）",
    "CGROUP_MEM_NEAR_LIMIT": "容器内存接近限额",
    "OOM_KILL_EVENTS": "进程被 OOM 杀死",
    "DISK_USED_HIGH": "磁盘空间不足",
    "DISK_INODE_HIGH": "inode 耗尽",
    "DISK_FILL_FORECAST": "磁盘空间将耗尽（预测）",
    "INODE_FILL_FORECAST": "inode 将耗尽（预测）",
    "DISK_SUDDEN_JUMP": "磁盘用量跳变",
    "BIG_FILE": "存在超大文件",
    "LOAD_HIGH": "负载过高",
    "FD_USAGE_HIGH": "文件描述符接近上限",
    "TCP_TIME_WAIT_HIGH": "TIME_WAIT 连接过多",
    "THERMAL_HIGH": "温度过高",
    "THERMAL_RISING": "温度持续上升（预测）",
}


def _compat_same_host(a, b, snap):
    # 单机快照里这一条恒真，返回空说明（不显示无信息量的"同一台机器"）。
    # 留成函数是为了将来接多机时只改这里：那时它要真的比较 host/拓扑。
    return True, ""


def _compat_file_on_mount(a, b, snap):
    m = a.get("mount")
    if not m:
        return False, ("无法判断该文件属于哪个挂载点（快照里没有可匹配的挂载点）→ "
                       "**不做归因**，避免把空间相邻当因果")
    mb = b.get("mount")
    if mb and _norm_path(mb) != _norm_path(m):
        return False, f"文件在 {m}，该发现属于 {mb} → 不是同一个挂载点"
    share = a.get("share_pct")
    if share is None:
        extra = "；占比**未核验**（缺文件大小或该挂载点容量）"
    else:
        extra = (f"；**已核验占比 {share}%**（文件大小 ÷ 该挂载点容量，都是采集到的硬数据）"
                 " —— 占用量的大头，优先处理，但「占比大」仍不等于是「原因」")
    return True, f"文件位于挂载点 {m}{extra}"


COMPAT = {"same_host": _compat_same_host, "file_on_mount": _compat_file_on_mount}


def describe(finding, snap=None):
    """finding → 收敛用的节点（实体、挂载点）。纯函数，不改原对象。"""
    rule = finding.get("rule") or ""
    spec = ENTITY_RULES.get(rule)
    if spec is None:
        # 未知规则（将来新加的）→ 自成实体，**保守不合并**（宁可少合并，不可乱合并）
        entity = ("rule", rule)
    else:
        kind, src = spec
        entity = (kind, finding.get("subject") if src == "subject" else src)
    node = {"rule": rule, "entity": entity,
            "severity": finding.get("severity", "info"),
            "subject": finding.get("subject", ""), "finding": finding}
    if spec and spec[0] == "file":
        node["mount"] = mount_of_path(finding.get("subject"), snap)
        node["share_pct"] = (file_share_pct(finding.get("subject"), node["mount"], snap)
                             if node["mount"] else None)
    elif spec and spec[0] == "mount":
        node["mount"] = finding.get("subject")
    return node


def _entity_text(entity):
    kind, val = entity
    if kind == "rule":
        return f"未知规则 {val}（保守：不与其他发现合并）"
    if kind == "host" or kind == "cgroup":
        return ENTITY_LABELS.get((kind, val), f"{kind}:{val}")
    if kind == "mount":
        return f"挂载点 {val}"
    if kind == "file":
        return f"文件 {val}"
    return f"{kind}:{val}"


def _edge_reason_text(edge):
    s = "机制上必然" if edge["strength"] == "strong" else "可解释但非唯一（未核验）"
    return f"{s}：{edge['reason']}"


# ══════════════════════════════════════════════════════════════════
# ③ 收敛：并查集 + 根因判定
# ══════════════════════════════════════════════════════════════════
class _UF:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def _sort_key(node):
    return (SEVERITY_ORDER.get(node["severity"], 9), node["rule"], str(node["subject"]))


def converge(findings, snap=None, edges=None):
    """发现 → 事件。返回 (events, report)。

    **归组只由两件事决定**（其余都是注释，不是黏合剂）：
      ① **强因果边**（`strong`）：机制上必然 → 归组，并允许断言根因
      ② **同一实体**：同一个资源只开一张单
    **弱因果边（`plausible`）不参与归组。** 这是实测踩到后改的：
    `温度→负载` 与 `内存→负载` 两条弱边一起做传递闭包，把「内存不足」和「温度过高」
    并成了一个事件、还写成同一条连带链 —— 而它俩很可能毫无关系。
    弱边只配产出**事件之间的交叉引用**（「这条可能与 EVT-2 有关，未核验」），人自己判断。
    ➡️ 原则：**少合并是少看一条，错合并是看错方向** —— 后者更贵。
    """
    findings = list(findings or [])
    nodes = [describe(f, snap) for f in findings]
    edge_defs = CAUSAL_EDGES if edges is None else edges
    uf = _UF(len(nodes))

    links = []                       # 命中的边（含"命中了但条件不满足"的，用于解释为什么没关联）
    for e in edge_defs:
        for i, a in enumerate(nodes):
            if a["rule"] != e["cause"]:
                continue
            for j, b in enumerate(nodes):
                if b["rule"] != e["effect"]:
                    continue
                fn = COMPAT.get(e.get("compat"))
                ok, why = (True, "") if fn is None else fn(a, b, snap)
                rec = {"cause_idx": i, "effect_idx": j, "cause": a["rule"], "effect": b["rule"],
                       "strength": e.get("strength", "plausible"),
                       "reason": e.get("reason", ""), "linked": ok, "why": why}
                links.append(rec)
                if ok and rec["strength"] == "strong":
                    uf.union(i, j)            # ★ 只有强边参与归组

    # ── 同一实体：同一个资源只开一张单（弱归组，不做因果断言）──
    by_entity = {}
    for i, n in enumerate(nodes):
        by_entity.setdefault(n["entity"], []).append(i)
    for ent, idxs in by_entity.items():
        for k in idxs[1:]:
            uf.union(idxs[0], k)

    comps = {}
    for i in range(len(nodes)):
        comps.setdefault(uf.find(i), []).append(i)

    events = []
    for _k, members in comps.items():
        members.sort(key=lambda i: _sort_key(nodes[i]))
        in_comp = [e for e in links
                   if e["cause_idx"] in members and e["effect_idx"] in members]
        events.append(_build_event(nodes, members, in_comp, findings))
    events.sort(key=lambda e: (SEVERITY_ORDER.get(e["severity"], 9), e["entities"][0]
                               if e["entities"] else "", e["title"]))
    annotate(events)
    link_events(events, links)

    total_members = sum(len(e["members"]) for e in events)
    report = {
        "findings": len(findings),
        "events": len(events),
        "compression": round(len(findings) / len(events), 2) if events else 0.0,
        "integrity_ok": total_members == len(findings),
        "integrity_detail": (f"发现 {len(findings)} 条 → 事件成员合计 {total_members} 条"
                             + ("（一致，没有告警被吃掉）" if total_members == len(findings)
                                else "（**不一致：有发现丢失或重复，这是 bug**）")),
        "roots_asserted": sum(1 for e in events if e["root"]),
        "candidates_only": sum(1 for e in events if not e["root"] and e.get("candidate_root")),
        "parallel": sum(1 for e in events if e["kind"] in ("parallel", "state_forecast")
                        and len(e["members"]) > 1),
        "cross_refs": sum(len(e.get("related") or []) for e in events),
    }
    return events, report


def link_events(events, links):
    """弱因果边 → 事件之间的交叉引用（**只提示，不合并**）。"""
    owner = {}
    for e in events:
        for i in e.get("_members_idx", []):
            owner[i] = e["event_id"]
    refs = {e["event_id"]: [] for e in events}
    for l in links:
        if not l["linked"] or l["strength"] != "plausible":
            continue
        a, b = owner.get(l["cause_idx"]), owner.get(l["effect_idx"])
        if a is None or b is None or a == b:
            continue                     # 同事件内的弱边：在事件内部说明即可
        # 弱边要说清两件事：领域理由（reason）+ **这次核验到了什么**（why，
        # 例如「已核验占比 21%」或「无法判断该文件属于哪个挂载点」）。少了后者就成了空话。
        why = (l.get("why") or "").strip()
        tail = f"｜核验：{why}" if why else ""
        base = {"cause": l["cause"], "effect": l["effect"], "reason": l["reason"],
                "compat": why}
        refs[a].append(dict(base, other_event=b,
                            text=f"本事件的 {l['cause']} 可能是 {b} 里 {l['effect']} 的原因"
                                 f"（**未断言**）：{l['reason']}{tail}"))
        refs[b].append(dict(base, other_event=a,
                            text=f"{a} 里的 {l['cause']} 可能解释了本事件的 {l['effect']}"
                                 f"（**未断言**）：{l['reason']}{tail}"))
    for e in events:
        seen, uniq = set(), []
        for r in sorted(refs[e["event_id"]], key=lambda r: (r["other_event"], r["cause"])):
            if r["text"] in seen:
                continue
            seen.add(r["text"])
            uniq.append(r)
        e["related"] = uniq
        e.pop("_members_idx", None)      # 内部用的下标不外泄
    return events



FORECAST_RULES = {"DISK_FILL_FORECAST", "INODE_FILL_FORECAST", "MEM_DEGRADING",
                  "THERMAL_RISING"}


def _member_view(node):
    return {"rule": node["rule"], "severity": node["severity"], "subject": node["subject"],
            "evidence": node["finding"].get("evidence", ""),
            "suggestion": node["finding"].get("suggestion", "")}


def _same_problem_note(rules):
    """所有两两组合都在 SAME_PROBLEM_PAIRS 里 → 这一组是「同一问题的现状+预测」。"""
    notes, covered = [], True
    for a in range(len(rules)):
        for b in range(a + 1, len(rules)):
            t = SAME_PROBLEM_PAIRS.get(frozenset({rules[a], rules[b]}))
            if t:
                notes.append(t)
            else:
                covered = False
    return covered, list(dict.fromkeys(notes))


def _build_event(nodes, members, in_comp, findings):
    rules = [nodes[i]["rule"] for i in members]
    urules = sorted(set(rules))
    severity = min((nodes[i]["severity"] for i in members),
                   key=lambda s: SEVERITY_ORDER.get(s, 9))
    strong = [e for e in in_comp if e["strength"] == "strong"]
    strong_in, strong_out = {}, {}
    for e in strong:
        strong_out[e["cause_idx"]] = strong_out.get(e["cause_idx"], 0) + 1
        strong_in[e["effect_idx"]] = strong_in.get(e["effect_idx"], 0) + 1

    root, root_idx, co_roots, candidate, note, kind = None, None, [], None, "", "single"
    if strong:
        kind = "causal"
        roots = [i for i in members if strong_out.get(i) and not strong_in.get(i)]
        if len(roots) == 1:
            root_idx = roots[0]
            root = nodes[root_idx]
            note = "根因判定依据**机制上必然**的因果边（见 --explain-graph）"
        elif len(roots) > 1:
            co_roots = [nodes[i] for i in roots]
            note = f"{len(co_roots)} 个并列上游（都有强因果边指向下游），不做单根因断言"
        else:
            # 有强边却没有入度为 0 的点 → 成环。**图错了归因就是错的**，所以拒绝断言
            note = "**检测到强因果边成环**：不做根因断言（图配置有问题，请查 --check-graph）"
    elif len(members) > 1:
        covered, notes = _same_problem_note(urules)
        if covered and notes:
            kind = "state_forecast"
            note = "；".join(notes)
        else:
            kind = "parallel"
            note = "同一资源上的多个问题，**无因果边 → 并列**（不是一条链，要分别处理）"
    # 同事件内的弱边：只给"候选"，明说没核验
    if not strong and in_comp:
        all_out, all_in = {}, {}
        for e in in_comp:
            all_out[e["cause_idx"]] = all_out.get(e["cause_idx"], 0) + 1
            all_in[e["effect_idx"]] = all_in.get(e["effect_idx"], 0) + 1
        cands = [i for i in members if all_out.get(i) and not all_in.get(i)]
        if len(cands) == 1:
            candidate = nodes[cands[0]]
            note = "只有**弱因果边**（可解释但非唯一）→ 只给候选根因，**不断言**是它造成的"

    # ── 连带项：**必须有一条强入边**，否则它不是"连带" ──
    # （实测踩到：单成员事件被写成「连带 1 项，跟着根因一起消」——纯属胡说）
    cascade, cascade_idx = [], set()
    for i in members:
        if i == root_idx:
            continue
        if co_roots and any(nodes[i] is c for c in co_roots):
            continue
        expl = [e for e in strong if e["effect_idx"] == i]
        if not expl:
            continue
        cascade_idx.add(i)
        cascade.append(dict(_member_view(nodes[i]), explained_by=[
            {"cause": e["cause"], "strength": e["strength"], "reason": e["reason"],
             "why": e.get("why", "")} for e in expl]))

    # ── 并列项：既不是根因也不是连带 → 明确列出来，别混进连带里 ──
    # 单成员事件没有"并列"可言（它自己就是主体）；实测踩到：单条事件被写成
    # 「并列 1 项（不是这条链上的，要单独处理）」—— 自己和自己并列，纯属胡说。
    if len(members) == 1:
        parallel_members = []
    else:
        parallel_members = [dict(_member_view(nodes[i])) for i in members
                            if i != root_idx and i not in cascade_idx
                            and not (co_roots and any(nodes[i] is c for c in co_roots))
                            and not (candidate is not None and nodes[i] is candidate)]

    vanished = ""
    if "OOM_KILL_EVENTS" in urules and not ({"MEM_AVAILABLE_LOW", "CGROUP_MEM_NEAR_LIMIT"}
                                            & set(urules)):
        vanished = ("快照里没有内存不足的证据，但 OOM 记录在 —— **根因可能已经消失**"
                    "（进程被杀后内存恢复了）；查 dmesg 确认被杀进程")

    # ── 标题 ──
    n_casc, n_par = len(cascade), len(parallel_members)
    if root or co_roots or candidate:
        pick = (root or (co_roots[0] if co_roots else candidate))["rule"]
        title = TITLES.get(pick, pick)
        suffix = []
        if n_casc:
            suffix.append(f"引发 {n_casc} 项连带")
        if n_par:
            suffix.append(f"另有 {n_par} 项同资源问题")
        if suffix:
            title += "（" + "，".join(suffix) + "）"
    elif kind == "state_forecast":
        state = next((r for r in urules if r not in FORECAST_RULES), urules[0])
        title = TITLES.get(state, state) + "（含趋势预测）"
    elif kind == "parallel":
        title = f"{_entity_text(nodes[members[0]]['entity'])} 上的 {len(members)} 个独立问题"
    else:
        title = TITLES.get(urules[0], urules[0])

    # ── 先做什么 ──
    if root:
        action = root["finding"].get("suggestion", "")
    elif co_roots:
        action = "并列上游都要看：" + "；".join(
            f"{c['rule']} → {c['finding'].get('suggestion', '')}" for c in co_roots)
    elif candidate:
        action = candidate["finding"].get("suggestion", "")
    elif len(members) == 1:
        action = nodes[members[0]]["finding"].get("suggestion", "")   # 单条：就是它自己的建议
    else:
        first = nodes[members[0]]["finding"]
        action = f"先处理最严重的 {first.get('rule')}：{first.get('suggestion', '')}"
    if kind == "parallel" and n_par:
        action += "｜⚠ 并列项要**分别**处理（它们不是一条链，修一个不会让另一个消失）"
    if kind == "state_forecast":
        action += "｜按预测给的时间预算安排，别等阈值被击穿"
    if vanished:
        action = vanished + "｜" + action

    dev_notes = []
    for a in range(len(members)):
        for b in range(a + 1, len(members)):
            t = NON_CAUSAL_NOTES.get(frozenset({nodes[members[a]]["rule"],
                                                nodes[members[b]]["rule"]}))
            if t:
                dev_notes.append(t)

    return {
        "event_id": "",                      # 排序后再编号
        "severity": severity,
        "kind": kind,
        "title": title,
        "entity": _entity_text(nodes[members[0]]["entity"]),
        "entities": sorted({_entity_text(nodes[i]["entity"]) for i in members}),
        "rules": urules,
        "root": dict(_member_view(root), verified=True) if root else None,
        "co_roots": [dict(_member_view(c), verified=True) for c in co_roots],
        "candidate_root": (dict(_member_view(candidate), verified=False)
                           if candidate else None),
        "attribution_note": note,
        "vanished_root_note": vanished,
        "non_causal_notes": dev_notes,
        "cascade": cascade,
        "parallel_members": parallel_members,
        "members": [{"rule": nodes[i]["rule"], "severity": nodes[i]["severity"],
                     "subject": nodes[i]["subject"],
                     "evidence": nodes[i]["finding"].get("evidence", "")} for i in members],
        "evidence_links": [{"cause": e["cause"], "effect": e["effect"],
                            "strength": e["strength"], "reason": e["reason"],
                            "compat": e.get("why", "")} for e in in_comp],
        "next_action": action,
        "_members_idx": members,             # 内部用：给 link_events 定位，外泄前 pop
    }



def annotate(events):
    """给事件编号（EVT-1…）。收敛率越低（事件数少）编号越靠前=越该先看。"""
    for i, e in enumerate(events, 1):
        e["event_id"] = f"EVT-{i}"
    return events


# ══════════════════════════════════════════════════════════════════
# ④ 渲染
# ══════════════════════════════════════════════════════════════════
def render(events, report, width=86):
    L = [f"【告警收敛】{report['findings']} 条发现 → {report['events']} 个事件"
         f"（压缩 {report['compression']}×）"
         f"｜断言根因 {report['roots_asserted']}｜并列/同源 {report['parallel']}"
         f"｜跨事件弱关联 {report.get('cross_refs', 0)}"]
    if not report.get("integrity_ok", True):
        L.append(f"  ⚠ 完整性自检失败：{report['integrity_detail']}")
    if not events:
        L.append("  （没有发现需要收敛 —— 只代表**已采集到的维度**正常）")
        return "\n".join(L)
    for e in events:
        L.append("")
        L.append(f"  [{e['severity']}] {e['event_id']} · {e['title']}")
        L.append(f"      资源：{'、'.join(e['entities'])}｜规则：{', '.join(e['rules'])}")
        if e["root"]:
            r = e["root"]
            L.append(f"      ▸ 根因（已断言）：{r['rule']}（{r['subject']}）{r['evidence']}")
        if e["co_roots"]:
            L.append("      ▸ 并列上游：" + "；".join(f"{c['rule']}（{c['evidence']}）"
                                                      for c in e["co_roots"]))
        if e["candidate_root"]:
            c = e["candidate_root"]
            L.append(f"      ▸ 候选根因（**未核验，不断言**）：{c['rule']}（{c['subject']}）"
                     f"{c['evidence']}")
        if e["attribution_note"]:
            L.append(f"      ▸ 归因说明：{e['attribution_note']}")
        if e["cascade"]:
            L.append(f"      ▸ 连带 {len(e['cascade'])} 项（跟着根因一起消，别单独处理）：")
            for c in e["cascade"]:
                why = "；".join(f"{x['cause']}[必然]" for x in c["explained_by"])
                L.append(f"          · {c['rule']}｜{c['subject']}：{c['evidence']}   ← {why}")
        if e["parallel_members"]:
            L.append(f"      ▸ 并列 {len(e['parallel_members'])} 项"
                     f"（**不是这条链上的，要单独处理**）：")
            for c in e["parallel_members"]:
                L.append(f"          · {c['rule']}｜{c['subject']}：{c['evidence']}")
        for n in e["non_causal_notes"]:
            L.append(f"      ⚑ {n}")
        for r in (e.get("related") or []):
            L.append(f"      ↔ 与 {r['other_event']} 可能相关：{r['text']}")
        L.append(f"      ▸ 先做：{e['next_action']}")
    L.append("")
    L.append("  ⚠ 收敛只做**归组与归因**，不删除任何发现；抑制误报要走反馈闭环（另一件事）")
    L.append("  ⚠ 边界：本收敛基于**单机单次快照**；跨机时序聚类需要中心侧带时间戳的告警流（未接）")
    return "\n".join(L)


def render_for_llm(events, report, fold_min=3, snap=None):
    """给模型的紧凑版：只给事件表，不给 N 条平铺发现。

    这就是收敛的**省钱价值**：模型的输入从「N 条原始发现」压成「事件表」，
    而且**因果已经由程序判定好了**，模型只做措辞与优先级 —— 它不需要（也不该）自己推理因果。

    ★ 实测踩到的坑（必须有资格叫优化就得真变短）：第一版把每个事件的「非因果说明」和
    「弱关联理由」全文重复，结果在 33 条发现 / 22 个事件的场景下，收敛输入比原始输入
    **还长 26.9%**。所以这里做三件事：
      ① 弱关联只写 `EVT-x(弱)`，不重复领域理由（事件内部已经说明过）
      ② 完全相同的「非因果说明」全局去重，挪到末尾的通用提醒
      ③ **结构相同的事件折叠**：同样的 kind + 同样的规则集合 + 同样的严重度 →
         合并成一条，列出全部 event_id 与每个事件的证据。折叠只压显示，**不减少事件**。

    `snap` 只用来补一行**机器环境摘要**（核数/容器/平台）—— 原始的完整快照 dump 不进来，
    因为那部分信息在每条发现的 evidence 里已经有数字了。想要完整上下文就用 `--llm-input full`。
    """
    L = [f"收敛结果：{report['findings']} 条发现 → {report['events']} 个事件"
         f"（发现总数未减少，只是归组；不要漏报任何事件）"]
    if snap:
        env = snap.get("environment") or {}
        up = snap.get("uptime") or {}
        plat = str(env.get("platform") or "?")
        # 平台串可能是整行 `uname -a`（很长），截断：模型只需要知道是什么系统
        if len(plat) > 40:
            plat = plat[:40] + "…"
        bits = [f"主机 {snap.get('host')}", f"平台 {plat}",
                f"核数 {env.get('ncpu')}", f"容器 {env.get('in_container')}"]
        if up.get("available"):
            bits.append(f"运行 {up['seconds'] / 86400:.1f} 天")
        L.append("环境：" + "｜".join(str(b) for b in bits))
    groups, order = {}, []
    for e in events:
        key = (e["kind"], tuple(e["rules"]), e["severity"])
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(e)
    footnotes = []
    for key in order:
        g = groups[key]
        if len(g) >= fold_min:
            _render_llm_folded(L, g)
        else:
            for e in g:
                _render_llm_one(L, e)
        for e in g:
            footnotes.extend(e["non_causal_notes"])
    if footnotes:
        L.append("通用提醒（适用于上面同资源的事件）:")
        for n in dict.fromkeys(footnotes):
            L.append("  - " + n)
    return "\n".join(L)


def _render_llm_one(L, e):
    L.append(f"[{e['event_id']}|{e['severity']}] {e['title']}｜资源 {'、'.join(e['entities'])}")
    if e["root"]:
        L.append(f"  根因(程序判定,机制必然): {e['root']['rule']} {e['root']['evidence']}")
    if e["co_roots"]:
        L.append("  并列上游: " + "；".join(f"{c['rule']} {c['evidence']}" for c in e["co_roots"]))
    if e["candidate_root"]:
        L.append(f"  候选根因(未核验,不得写成结论): {e['candidate_root']['rule']} "
                 f"{e['candidate_root']['evidence']}")
    for c in e["cascade"]:
        L.append(f"  连带(随根因消): {c['rule']} {c['evidence']}")
    for c in e["parallel_members"]:
        L.append(f"  并列(单独处理): {c['rule']} {c['evidence']}")
    if e.get("related"):
        L.append("  可能相关(未核验): " + "，".join(
            f"{r['other_event']}:{r['cause']}→{r['effect']}" for r in e["related"]))
    if e["vanished_root_note"]:
        L.append("  注意: " + e["vanished_root_note"])
    L.append(f"  建议: {e['next_action']}")


def _render_llm_folded(L, g):
    """结构相同的 N 个事件 → 一条。**event_id 全部列出**（模型不能漏任何一个）。"""
    e0 = g[0]
    ids = ",".join(e["event_id"] for e in g)
    L.append(f"[{ids}|{e0['severity']}] 同类 {len(g)} 个事件：{e0['title']}")
    L.append(f"  结构（每个都一样）：" + "；".join(
        ([f"根因 {e0['root']['rule']}"] if e0["root"] else [])
        + [f"连带 {c['rule']}" for c in e0["cascade"]]
        + [f"并列 {c['rule']}" for c in e0["parallel_members"]]))
    L.append("  各事件证据：")
    for e in g:
        L.append(f"    {e['event_id']} {'、'.join(e['entities'])}: " + "；".join(
            f"{m['rule']} {m['evidence']}" for m in e["members"]))
    rel = [f"{r['other_event']}:{r['cause']}→{r['effect']}"
           for e in g for r in (e.get("related") or [])]
    if rel:
        L.append("  可能相关(未核验): " + "，".join(dict.fromkeys(rel)))
    L.append(f"  建议（同结构，可批量处置）: {e0['next_action']}")

    return "\n".join(L)



# ══════════════════════════════════════════════════════════════════
# ⑤ 多机「共性嫌疑」——对"几十台机器同时报警"的诚实近似
# ══════════════════════════════════════════════════════════════════
def fleet_cluster(per_host, min_hosts=3):
    """[(host, findings)] → 同一规则在多台机器同时触发的簇。

    **它证明不了什么**：单机快照之间没有共同时钟，我们只能说"这批巡检里 N 台机器都报了同一条规则"。
    因此输出一律标注为**共性嫌疑**，并给出"要不要怀疑共性原因"的判断依据（台数 + 严重度），
    而不是"根因是网络"这种我们没有证据的话。
    """
    agg = {}
    for host, findings in per_host:
        for f in findings or []:
            a = agg.setdefault(f.get("rule"), {"hosts": set(), "severity": f.get("severity"),
                                               "sample_subject": f.get("subject")})
            a["hosts"].add(host)
    out = []
    n_hosts = len(per_host)
    for rule, a in agg.items():
        if len(a["hosts"]) < min_hosts:
            continue
        ratio = len(a["hosts"]) / n_hosts if n_hosts else 0
        out.append({
            "rule": rule, "n_hosts": len(a["hosts"]), "hosts": sorted(a["hosts"]),
            "severity": a["severity"], "sample_subject": a["sample_subject"],
            "ratio_pct": round(ratio * 100, 1),
            "verdict": "共性嫌疑（同一规则在多台机器同时出现）",
            "note": ("建议先查**共性因素**：同一批发布/同一上游依赖/同一网络/同一时间窗的变更；"
                     "⚠ 本判断基于「同一批巡检窗口」，**没有跨机时钟对齐**，不能证明同一时刻"),
        })
    out.sort(key=lambda x: (-x["n_hosts"], SEVERITY_ORDER.get(x["severity"], 9), x["rule"]))
    return out


def render_fleet(clusters, n_hosts):
    if not clusters:
        return f"【多机共性】{n_hosts} 台机器中，没有同一规则达到 3 台的簇（单机问题更可能）"
    L = [f"【多机共性】{n_hosts} 台机器中，以下规则在多台同时出现（**疑似共性原因**）："]
    for c in clusters:
        L.append(f"  · {c['rule']}：{c['n_hosts']}/{n_hosts} 台（{c['ratio_pct']}%）"
                 f"｜{c['severity']}｜例：{c['sample_subject']}")
    L.append("  ⚠ " + clusters[0]["note"])
    return "\n".join(L)


# ══════════════════════════════════════════════════════════════════
# ⑥ 因果图自审（让人能推翻我们）
# ══════════════════════════════════════════════════════════════════
def explain_graph(edges=None):
    edge_defs = CAUSAL_EDGES if edges is None else edges
    L = ["【因果图】（写死的领域知识，人可以质疑和推翻；strong 才允许断言根因）"]
    for e in edge_defs:
        tag = "必然" if e.get("strength") == "strong" else "可解释"
        L.append(f"  {e['cause']} → {e['effect']}  [{tag}]  条件：{e.get('compat')}")
        L.append(f"      理由：{e.get('reason', '')}")
    L.append("")
    L.append("【已知的反例：看着像一条链，其实不是】")
    for pair, note in NON_CAUSAL_NOTES.items():
        L.append(f"  {' ✗ ' + ' / '.join(sorted(pair))}：{note}")
    L.append("")
    L.append("【同一问题的现状+预测（合并成一条，不是因果）】")
    for pair, note in SAME_PROBLEM_PAIRS.items():
        L.append(f"  = {' + '.join(sorted(pair))}：{note}")
    return "\n".join(L)


def check_graph(edges=None):
    """自检：① 强因果子图必须无环 ② 边两端的规则都得在实体表里有归属
    ③ 不许出现自己指向自己。**图错了 → 归因就是错的**，所以这条要进 CI。"""
    edge_defs = CAUSAL_EDGES if edges is None else edges
    problems = []
    adj, strong_adj = {}, {}
    for e in edge_defs:
        c, f_ = e.get("cause"), e.get("effect")
        if not c or not f_:
            problems.append(f"边缺 cause/effect：{e}")
            continue
        if c == f_:
            problems.append(f"自环：{c}")
        if e.get("strength") not in ("strong", "plausible"):
            problems.append(f"strength 只能是 strong/plausible：{c}→{f_} 给了 {e.get('strength')}")
        if e.get("compat") not in COMPAT:
            problems.append(f"未知 compat：{c}→{f_} 用了 {e.get('compat')}")
        for r in (c, f_):
            if r not in ENTITY_RULES:
                problems.append(f"规则 {r} 不在 ENTITY_RULES 里（无法归实体）")
        adj.setdefault(c, set()).add(f_)
        if e.get("strength") == "strong":
            strong_adj.setdefault(c, set()).add(f_)
    # 强子图找环（DFS）
    color = {}

    def dfs(u):
        color[u] = 1
        for v in strong_adj.get(u, ()):
            if color.get(v) == 1:
                problems.append(f"强因果子图成环：{u}→{v}")
            elif color.get(v, 0) == 0:
                dfs(v)
        color[u] = 2

    for u in list(strong_adj):
        if color.get(u, 0) == 0:
            dfs(u)
    # 所有能触发的规则都该有标题（渲染层不能因为缺标题就掉字）
    for r in ENTITY_RULES:
        if r not in TITLES:
            problems.append(f"规则 {r} 没有中文标题（事件标题会退化成规则名）")
    # 反例表不能自相矛盾：既说互不解释，又给强因果边
    for pair in NON_CAUSAL_NOTES:
        for e in edge_defs:
            if e.get("strength") == "strong" and frozenset({e["cause"], e["effect"]}) == pair:
                problems.append(f"{set(pair)} 被标为互不解释，却又有一条 strong 因果边 → 矛盾")
    return problems


# ══════════════════════════════════════════════════════════════════
# CLI：主要给排障/评审用（主链路在 day42_live.py 里）
# ══════════════════════════════════════════════════════════════════
def _main(argv=None):
    ap = argparse.ArgumentParser(description="告警收敛（单机单次快照内）")
    ap.add_argument("--from", dest="files", action="append", default=[],
                    help="快照 .json 或 collect_linux.sh 文本 dump（可多次 → 多机共性）")
    ap.add_argument("--explain-graph", action="store_true", help="打印因果图（给人审计）")
    ap.add_argument("--check-graph", action="store_true", help="自检因果图（进 CI）")
    ap.add_argument("--json", dest="json_out", default=None)
    ap.add_argument("--min-hosts", type=int, default=3, help="多机共性嫌疑的最少台数")
    args = ap.parse_args(argv)

    if args.check_graph:
        probs = check_graph()
        if probs:
            print("因果图自检**未通过**：")
            for p in probs:
                print(f"  ✗ {p}")
            return 2
        print(f"因果图自检通过：{len(CAUSAL_EDGES)} 条边，"
              f"强边 {sum(1 for e in CAUSAL_EDGES if e['strength'] == 'strong')} 条，无环，规则齐全")
        return 0
    if args.explain_graph:
        print(explain_graph())
        return 0
    if not args.files:
        ap.error("需要 --from 至少一个快照，或 --explain-graph / --check-graph")

    import collect as C
    per_host, out = [], []
    for p in args.files:
        if p.lower().endswith(".json"):
            snap = C.load(p)
        else:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                snap = C.parse_linux_dump(f.read())
        fs = C.evaluate(snap)
        host = snap.get("host") or os.path.basename(p)
        ev, rep = converge(fs, snap)
        print("=" * 86)
        print(f"{host}（{p}）")
        print(render(ev, rep))
        per_host.append((host, fs))
        out.append({"host": host, "report": rep, "events": ev, "findings": fs})

    if len(per_host) > 1:
        clusters = fleet_cluster(per_host, args.min_hosts)
        print("=" * 86)
        print(render_fleet(clusters, len(per_host)))
        out.append({"_fleet": clusters})

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump({"hosts": out}, f, ensure_ascii=False, indent=2)
        print(f"结构化结果：{args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
