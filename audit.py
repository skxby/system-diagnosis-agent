#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""作品 3 · 安全审计日志与工具级作用域 —— audit.py

**为什么要有这个模块**（企业调研里对 MCP 的那句原话）：

    MCP server 是**新的信任边界**，不是协议适配器。
    —— 要工具级 Read/Write/Admin 作用域（而不是服务级白名单）+ 结构化审计日志（谁在何时碰了什么）

一个"能给模型读日志"的服务，实际上就是**一个能读你磁盘的远程入口**。
出了事要能回答三个问题：**谁、在什么时候、读了什么**。答不上来，这个服务就不该上生产。

═══════════════════════════════════════════════════════════════════
六条不让步的设计
═══════════════════════════════════════════════════════════════════
**① 作用域按「工具」声明，不按「服务」声明，而且默认拒绝。**
   服务级白名单（"这个 server 允许访问日志目录"）粒度太粗：一旦里面多了一个
   能删文件的工具，白名单照样放行。所以每个工具自己声明需要 `read` / `write` / `admin`。
   **忘记声明作用域的工具一律按 `admin` 处理并拒绝**（fail-closed）——
   加工具时忘了声明，结果是"被拦住并被审计"，而不是"静默放行"。

**② 审计日志记「读了什么」，绝不记「读到了什么」。**
   日志内容（可能是密码、个人信息、客户数据）**一个字都不进审计文件** ——
   否则审计日志本身就成了新的泄露面。只记：工具名、参数（脱敏+截断）、目标资源、
   结果状态、字节数/行数、耗时。参数里的密钥形态字符串一律抹成 `***`。

**③ 审计记录必须能查出被改动过。**
   每条记录带 `prev`（上一条的哈希）与 `h`（本条内容的哈希），形成**哈希链**。
   改动/删除任何一条都会断链，`verify()` 能指到具体是第几条断的。
   ⚠ 诚实边界：这**不是**防篡改（能写这个文件的人也能整份重写），
   它只让篡改**可被发现**。真正的防篡改需要把链锚定到外部（远端的日志服务/只追加存储）。

**④ 拒绝也要审计（而且是重点）。**
   只记成功调用的审计日志几乎没有价值 —— 有价值的恰恰是**被拒绝的越界尝试**：
   谁在什么时候试图读 `.env`。所以 `allowed=false` 的记录带 `deny_reason`。

**⑤ 日志有上限、会轮转。**
   审计日志不许无界增长（把磁盘吃满的审计日志是另一种事故）。
   轮转时把上一条的哈希**带过去**，链不断。

**⑥ 身份必须诚实。**
   stdio 传输**没有认证** —— 能启动这个进程的人就拥有这个进程的身份。
   所以 `principal` 写的是「本地 OS 用户（未认证）」，而不是假装成某个已认证主体。
   真正的认证授权要 HTTP 传输 + OAuth 2.1 + PKCE（**那是 P2，本地 stdio 不需要也不该假装有**）。

跑法：
    python audit.py --report            # 人类可读的审计报告（谁/工具/拒绝/目标）
    python audit.py --verify            # 校验哈希链（查找篡改）
    python audit.py --self-test         # 演示：未声明作用域的工具会被默认拒绝
"""
import os
import io
import re
import sys
import json
import time
import hmac
import socket
import hashlib
import getpass
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_AUDIT = os.path.join(HERE, "snapshots", "audit.jsonl")
MAX_BYTES = 5 * 1024 * 1024          # 单文件 5MB 就轮转（审计日志不许吃满磁盘）
KEEP_FILES = 5                       # 保留最近 5 个轮转文件
ARG_MAX_CHARS = 300                  # 参数截断：审计日志不是数据仓库
IDENTITY = "local-os-user（未认证）"   # 见纪律⑥

# ── 作用域：值越大权限越高 ─────────────────────────────────────
SCOPES = {"read": 0, "write": 1, "admin": 2}
RANK_SCOPE = {v: k for k, v in SCOPES.items()}

# ── 工具 → 所需作用域（**服务里加新工具必须在表里声明**；不声明就按 admin 拒绝）──
TOOL_SCOPES = {
    "list_files": "read",
    "read_log": "read",
    "search_log": "read",
    "read_log_range": "read",
    "check_disk": "read",
    "system_snapshot": "read",
    "top_files_by_size": "read",
    "readable_logs": "read",          # MCP resource
    "live_diagnose": "read",          # day42_live 的一次体检
}

# ── 密钥形态（命中就抹掉；宁可多抹，不可漏抹）────────────────────
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)(api[_-]?key|secret|passwd|password|token)\s*[=:]\s*\S+"),
    re.compile(r"[A-Za-z0-9+/]{40,}={0,2}"),        # 长 base64（密钥/证书常见形态）
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),            # 长十六进制（哈希/密钥）
]
REDACTED = "***REDACTED***"


def redact(value, max_chars=ARG_MAX_CHARS):
    """把参数里的密钥形态抹掉并截断。

    **不做的事**：不改写路径（审计要的就是路径）、不记录内容（见纪律②）。
    """
    if value is None:
        return None
    if isinstance(value, bool) or isinstance(value, int) or isinstance(value, float):
        return value
    if isinstance(value, (list, tuple)):
        return [redact(v, max_chars) for v in value]
    if isinstance(value, dict):
        return {str(k): redact(v, max_chars) for k, v in value.items()}
    s = str(value)
    for pat in SECRET_PATTERNS:
        s = pat.sub(REDACTED, s)
    if len(s) > max_chars:
        s = s[:max_chars] + f"…（截断，原长 {len(str(value))}）"
    return s


def looks_sensitive(path):
    """路径是不是"敏感名"（审计里高亮它 —— 这正是最该被人看到的记录）。"""
    import fnmatch
    name = os.path.basename(str(path or "")).lower()
    pats = (".env", "*.env", "id_rsa*", "id_ed25519*", "*.pem", "*.key", "*.p12", "*.pfx",
            "*.sqlite", "*.db", "*secret*", "*credential*", "*password*", "*token*")
    return any(fnmatch.fnmatch(name, p) for p in pats)


def identity(caller=None):
    """诚实身份：本地 OS 用户 + 主机名；**明确标注未认证**。"""
    user = caller or os.environ.get("DSH_AUDIT_CALLER") or _safe_user()
    return {"caller": f"{user}@{_safe_host()}", "principal": IDENTITY,
            "transport": os.environ.get("DSH_MCP_TRANSPORT", "stdio")}


def _safe_user():
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


def _safe_host():
    try:
        return socket.gethostname()
    except Exception:
        return "unknown"


# ══════════════════════════════════════════════════════════════════
# 作用域闸门（fail-closed）
# ══════════════════════════════════════════════════════════════════
class ScopePolicy:
    """调用方被授予哪些作用域 + 每个工具需要什么作用域。

    `granted` 默认只有 `read` —— 与"我们刻意只做只读工具"一致。
    想放开写能力，必须**显式**给（`--grant read,write` 或环境变量），
    这样"多了一个能写的工具"不会在某天悄悄上线。
    """

    def __init__(self, granted=("read",), tool_scopes=None):
        self.granted = tuple(granted or ("read",))
        self.tool_scopes = dict(TOOL_SCOPES if tool_scopes is None else tool_scopes)
        bad = [g for g in self.granted if g not in SCOPES]
        if bad:
            raise ValueError(f"非法作用域：{bad}（只能是 {sorted(SCOPES)}）")

    @staticmethod
    def from_env(default=("read",)):
        raw = os.environ.get("DIAG_SCOPES") or os.environ.get("DSH_AUDIT_SCOPES")
        if not raw:
            return ScopePolicy(default)
        return ScopePolicy([s.strip() for s in raw.split(",") if s.strip()])

    def required(self, tool):
        """未声明的工具 → 按 admin（最严）处理。

        ⚠ 这是本模块最重要的一行：**忘记声明 = 被拒绝 + 被审计**，
        而不是"没写就随便放"。加工具的人会立刻在审计里看到自己的拒绝记录。
        """
        return self.tool_scopes.get(tool, "admin")

    def allow(self, tool):
        need = self.required(tool)
        declared = tool in self.tool_scopes
        if SCOPES[need] <= max((SCOPES[g] for g in self.granted), default=-1):
            return True, need, ""
        why = (f"工具 {tool} 需要 {need} 作用域，当前只授予 {list(self.granted)}"
               + ("" if declared else "；该工具**未在作用域表里声明**，按最严的 admin 处理"))
        return False, need, why

    def describe(self):
        return (f"授予作用域：{list(self.granted)}｜已声明工具 "
                f"{len(self.tool_scopes)} 个（未声明的一律按 admin 拒绝）")


# ══════════════════════════════════════════════════════════════════
# 审计日志：哈希链 + 轮转 + 脱敏
# ══════════════════════════════════════════════════════════════════
def _canonical(entry):
    """稳定序列化（排序键、紧凑分隔符）—— 哈希必须与格式无关地可复现。"""
    body = {k: v for k, v in entry.items() if k != "h"}
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(prev, entry):
    return hashlib.sha256((str(prev or "") + _canonical(entry)).encode("utf-8")).hexdigest()[:24]


class AuditLog:
    """只追加的结构化审计日志。每条带 `prev`/`h` 组成哈希链。"""

    def __init__(self, path=DEFAULT_AUDIT, max_bytes=MAX_BYTES, keep=KEEP_FILES,
                 caller=None, enabled=True):
        self.path = path
        self.max_bytes = max_bytes
        self.keep = keep
        self.enabled = enabled
        self._id = identity(caller)

    # ── 读 ──────────────────────────────────────────────────────
    def load(self, path=None):
        p = path or self.path
        if not os.path.exists(p):
            return []
        out = []
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    out.append({"_bad": True, "_raw": line[:200]})
                    continue
                out.append(rec)
        return out

    def last_hash(self):
        for rec in reversed(self.load()):
            if rec.get("h"):
                return rec["h"]
        return ""

    # ── 写 ──────────────────────────────────────────────────────
    def record(self, tool, args=None, ok=True, scope=None, allowed=True, deny_reason="",
               target=None, result_summary="", error=None, duration_ms=0, session="",
               extra=None):
        """记一条。返回记录（含 seq/h）；`enabled=False` 时只返回不落盘（测试/离线用）。"""
        if not self.enabled:
            return {"skipped": True, "tool": tool, "allowed": allowed}
        prev = self.last_hash()
        rec = {
            "seq": self._next_seq(),
            "ts": round(time.time(), 3),
            "iso": time.strftime("%Y-%m-%d %H:%M:%S"),
            **self._id,
            "session": session or os.environ.get("DSH_SESSION_ID", ""),
            "tool": tool,
            "scope": scope or "admin",
            "allowed": bool(allowed),
            "deny_reason": deny_reason or "",
            "args": redact(args or {}),
            "target": str(target) if target is not None else None,
            # ★ 只记"敏感名尝试"这个事实，**不记内容**（纪律②）
            "sensitive_attempt": bool(target and looks_sensitive(target)),
            "ok": bool(ok),
            "result_summary": result_summary,
            "error": redact(error, 200) if error else None,
            "duration_ms": round(float(duration_ms), 1),
            "prev": prev,
        }
        if extra:
            rec["extra"] = redact(extra)
        rec["h"] = _hash(prev, rec)
        self._write(rec)
        return rec

    def _next_seq(self):
        recs = self.load()
        n = 0
        for r in recs:
            if isinstance(r.get("seq"), int):
                n = max(n, r["seq"])
        return n + 1

    def _rotate_if_needed(self):
        try:
            if os.path.exists(self.path) and os.path.getsize(self.path) >= self.max_bytes:
                stamp = time.strftime("%Y%m%d-%H%M%S")
                os.replace(self.path, f"{self.path}.{stamp}")
                # 轮转后清掉最老的几个（保留 keep 个）
                others = sorted([f for f in os.listdir(os.path.dirname(self.path) or ".")
                                 if f.startswith(os.path.basename(self.path) + ".")])
                for old in others[:-self.keep] if len(others) > self.keep else []:
                    try:
                        os.remove(os.path.join(os.path.dirname(self.path) or ".", old))
                    except OSError:
                        pass
        except OSError:
            pass                    # 轮转失败不该让被审计的操作失败

    def _write(self, rec):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self._rotate_if_needed()
        # 轮转后新文件是空的，但**链不用额外处理**：这一条的 `prev` 已经记着
        # 轮转前最后一条的哈希（`record()` 在写之前就取好了），
        # 所以把 [旧文件, 新文件] 按顺序交给 verify() 依然能校验整条链。
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ── 校验 ────────────────────────────────────────────────────
    def verify(self, paths=None):
        """校验哈希链。返回 `(ok, problems, checked)`。

        ⚠ 诚实边界：这只证明"文件内自洽"，**不证明没被整份重写**
        （能做到后者的人也能重算整条链）。要真防篡改得把链锚定到外部。
        """
        paths = paths or [self.path]
        problems, checked, prev = [], 0, None
        for p in paths:
            for i, rec in enumerate(self.load(p), 1):
                where = f"{os.path.basename(p)}#{i}"
                if rec.get("_bad"):
                    problems.append(f"{where} 不是合法 JSON（文件被手改过？）")
                    continue
                if prev is not None and rec.get("prev") != prev:
                    problems.append(f"{where} 断链：记录的 prev={rec.get('prev')}，"
                                    f"但上一条的 h={prev}")
                expect = _hash(rec.get("prev"), rec)
                if rec.get("h") != expect:
                    problems.append(f"{where} 内容与哈希不符（**这条被改过**）")
                prev = rec.get("h")
                checked += 1
        return (not problems), problems, checked


# ══════════════════════════════════════════════════════════════════
# 闸门：唯一的调用入口（策略 + 审计 + 计时）
# ══════════════════════════════════════════════════════════════════
class Gate:
    """`gate.call(tool, fn, args)` —— 先判作用域、再执行、无论成败都记审计。

    为什么把三件事绑在一起：**只要有一处忘了记审计，审计日志就不再可信**。
    所以不许调用方"自己记得记"。
    """

    def __init__(self, policy=None, log=None):
        self.policy = policy or ScopePolicy.from_env()
        self.log = log or AuditLog()

    def call(self, tool, fn, kwargs=None, args=None, target=None, session="", summary=None):
        kwargs = kwargs or {}
        args = args if args is not None else {}
        allowed, need, why = self.policy.allow(tool)
        t0 = time.perf_counter()
        if not allowed:
            rec = self.log.record(tool, args=args, ok=False, scope=need, allowed=False,
                                  deny_reason=why, target=target, session=session,
                                  error="作用域不足")
            raise PermissionError(f"[审计] {why}（已记录 seq={rec.get('seq')}）")
        try:
            out = fn(**kwargs)
        except Exception as e:
            self.log.record(tool, args=args, ok=False, scope=need, allowed=True, target=target,
                            error=f"{type(e).__name__}: {e}", session=session,
                            duration_ms=(time.perf_counter() - t0) * 1000)
            raise
        self.log.record(tool, args=args, ok=True, scope=need, allowed=True, target=target,
                        session=session, duration_ms=(time.perf_counter() - t0) * 1000,
                        result_summary=summary(out) if summary else _summarize(out))
        return out


def _summarize(out):
    """结果摘要：**只记形状，不记内容**（纪律②）。

    ★ 我第一版写成了「开头：<前 80 字>」—— 那等于把日志内容抄进审计文件，
    正是纪律②禁止的事（**审计日志自己变成了新的泄露面**）。改成只记形状：
    字符数、行数，以及"是否出现错误关键词"这类**派生的事实**。
    """
    if out is None:
        return "None"
    s = str(out)
    info = [f"{len(s)} 字符"]
    lines = s.count("\n") + 1
    if lines > 1:
        info.append(f"{lines} 行")
    if re.search(r"(?i)\b(error|exception|failed|traceback|denied)\b", s):
        info.append("含错误关键词")
    return "｜".join(info)


# ══════════════════════════════════════════════════════════════════
# 报告
# ══════════════════════════════════════════════════════════════════
def report(log=None, limit=500):
    log = log or AuditLog()
    recs = [r for r in log.load() if not r.get("_bad")][-limit:]
    ok, problems, checked = (log.verify())
    by_tool, denied, sensitive = {}, [], []
    for r in recs:
        by_tool[r.get("tool")] = by_tool.get(r.get("tool"), 0) + 1
        if not r.get("allowed"):
            denied.append(r)
        if r.get("sensitive_attempt"):
            sensitive.append(r)
    L = [f"【审计报告】记录 {len(recs)} 条（校验 {checked} 条）"
         f"｜主体 {IDENTITY}｜传输 {log._id['transport']}"]
    L.append("  哈希链校验：" + ("✅ 完整（无改动痕迹）" if ok else f"❌ 发现问题 {len(problems)} 处"))
    for p in problems[:5]:
        L.append(f"    · {p}")
    if not recs:
        L.append("  （还没有审计记录）")
        return "\n".join(L)
    L.append("  按工具：" + "、".join(f"{k}×{v}" for k, v in
                                     sorted(by_tool.items(), key=lambda kv: -kv[1])))
    L.append(f"  被拒绝：{len(denied)} 条" + ("（**这是审计里最有价值的部分**）" if denied else ""))
    for r in denied[-5:]:
        L.append(f"    ✗ {r.get('iso')} {r.get('tool')}（需 {r.get('scope')}）：{r.get('deny_reason')}")
    if sensitive:
        L.append(f"  ⚠ 触碰敏感名的尝试：{len(sensitive)} 条")
        for r in sensitive[-5:]:
            L.append(f"    ! {r.get('iso')} {r.get('tool')} → {r.get('target')}"
                     f"（{'已拒绝' if not r.get('allowed') else '被沙箱拒绝' if not r.get('ok') else '放行'}）")
    L.append("  最近记录：")
    for r in recs[-6:]:
        L.append(f"    #{r.get('seq')} {r.get('iso')} {r.get('tool')}"
                 f"（{r.get('scope')}）{'OK' if r.get('ok') and r.get('allowed') else 'FAIL'}"
                 f"｜{r.get('result_summary') or r.get('error') or ''}")
    L.append("  ⚠ 边界：stdio 传输**没有认证**（principal=本地 OS 用户，未认证）；"
             "哈希链只让篡改**可被发现**，不是防篡改")
    return "\n".join(L)


# ══════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════
def _self_test():
    """演示 fail-closed：一个**未声明作用域**的工具会被默认拒绝并记审计。"""
    import tempfile
    tmp = tempfile.mkdtemp(prefix="audit_selftest_")
    log = AuditLog(os.path.join(tmp, "a.jsonl"))
    gate = Gate(ScopePolicy(granted=("read",)), log)
    print("策略：" + gate.policy.describe())
    out = gate.call("read_log", lambda: "日志内容", args={"path": "a.log"}, target="a.log")
    print(f"① 已声明的 read 工具 → 放行（返回 {len(out)} 字符）")
    try:
        gate.call("delete_everything", lambda: "删了", args={}, target="/")
        print("② 未声明的工具 → **居然放行了**（这是 bug）")
        return 1
    except PermissionError as e:
        print(f"② 未声明作用域的工具 → 默认拒绝：{e}")
    try:
        gate.call("write_config", lambda: "写了", args={}, target="x")
    except PermissionError:
        pass
    print("\n" + report(log))
    print(f"\n（自检用的临时审计文件在 {tmp}，不影响真实日志）")
    return 0


def _main(argv=None):
    ap = argparse.ArgumentParser(description="安全审计日志与工具级作用域")
    ap.add_argument("--report", action="store_true", help="打印审计报告")
    ap.add_argument("--verify", action="store_true", help="校验哈希链（查找篡改）")
    ap.add_argument("--self-test", action="store_true", help="演示 fail-closed 的作用域闸门")
    ap.add_argument("--audit", default=DEFAULT_AUDIT, help="审计文件路径")
    ap.add_argument("--grant", default=None, help="授予的作用域，如 read 或 read,write")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()
    policy = ScopePolicy([s.strip() for s in args.grant.split(",")]) if args.grant \
        else ScopePolicy.from_env()
    log = AuditLog(args.audit)
    if args.verify:
        ok, problems, checked = log.verify()
        if ok:
            print(f"哈希链校验通过：{checked} 条记录，未发现改动痕迹")
            return 0
        print(f"哈希链校验**未通过**（{len(problems)} 处）：")
        for p in problems:
            print(f"  ✗ {p}")
        return 2
    print(policy.describe())
    print(report(log))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(_main())
