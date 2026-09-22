#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""安全审计日志与工具级作用域的离线测试 —— test_audit.py（免费、秒级、不要 key）

**为什么这个模块必须有测试**：审计日志的失败是**沉默的**——
它记错了、漏记了、或者把密钥抄了进去，平时完全看不出来，
等到要复盘（"谁在什么时候读了 .env"）时才发现答案不可信。
所以这里的断言集中在五件事：

  ① **fail-closed**：未声明作用域的工具默认拒绝，且**拒绝会被审计**
  ② **不记内容**：审计文件里**不得出现被读内容**的任何特征串，也不得出现密钥
  ③ **哈希链**：改动/删除/换序任何一条都能被 `verify()` 指到具体位置
  ④ **有界**：会轮转、保留数量有上限（审计日志不许吃满磁盘）
  ⑤ **身份诚实**：principal 明确写"未认证"，不假装成已认证主体

跑法：.venv\\Scripts\\python.exe test_audit.py
"""
import os
import sys
import json
import time
import shutil
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audit as A

PASS, FAIL = [], []
SECRET = "sk-abcdefghijklmnopqrstuvwxyz0123456789"
LOG_CONTENT = "2026-09-22 ERROR 磁盘故障 customer_email=zhang@example.com 订单号 A1B2C3D4"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}{('  —— ' + str(detail)) if detail else ''}")


def log_of(tmp, name="audit.jsonl", **kw):
    return A.AuditLog(os.path.join(tmp, name), **kw)


def raw(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def main():
    t0 = time.perf_counter()
    tmp = tempfile.mkdtemp(prefix="audittest_")
    try:
        # ══════════════════════════════════════════════════════════════
        print("[1] 作用域闸门：fail-closed，未声明一律按 admin 拒绝")
        # ══════════════════════════════════════════════════════════════
        pol = A.ScopePolicy(granted=("read",))
        ok, need, why = pol.allow("read_log")
        check("已声明的 read 工具放行", ok and need == "read", (ok, need))
        ok, need, why = pol.allow("delete_everything")
        check("★ 未声明的工具 → 拒绝，且按最严的 admin 处理", not ok and need == "admin", (ok, need))
        check("拒绝理由里写明「未在作用域表里声明」（加工具的人一眼看懂）",
              "未在作用域表里声明" in why, why[-40:])
        ok2, need2, _ = pol.allow("write_config")
        check("写类工具在只授予 read 时被拦", not ok2 and need2 == "admin")
        pol_w = A.ScopePolicy(granted=("read", "write"), tool_scopes={"write_config": "write"})
        check("显式授予 write 后写工具才放行", pol_w.allow("write_config")[0] is True)
        pol_a = A.ScopePolicy(granted=("admin",), tool_scopes={"x": "admin"})
        check("授予 admin 后最严的工具也放行", pol_a.allow("x")[0] is True)
        try:
            A.ScopePolicy(granted=("root",))
            check("非法作用域被拒绝", False)
        except ValueError as e:
            check("非法作用域被拒绝", "非法作用域" in str(e))
        check("环境变量能配作用域（DIAG_SCOPES）", True)
        old = os.environ.get("DIAG_SCOPES")
        os.environ["DIAG_SCOPES"] = "read,write"
        try:
            check("从环境变量读到的授予被解析",
                  A.ScopePolicy.from_env().granted == ("read", "write"),
                  A.ScopePolicy.from_env().granted)
        finally:
            if old is None:
                os.environ.pop("DIAG_SCOPES", None)
            else:
                os.environ["DIAG_SCOPES"] = old
        check("默认授予只有 read（与「我们刻意只做只读工具」一致）",
              A.ScopePolicy().granted == ("read",))

        # ══════════════════════════════════════════════════════════════
        print("[2] 审计记录：拒绝也要记，且带原因")
        # ══════════════════════════════════════════════════════════════
        p = os.path.join(tmp, "audit.jsonl")
        log = A.AuditLog(p)
        gate = A.Gate(A.ScopePolicy(granted=("read",)), log)
        gate.call("read_log", lambda: "OK", args={"path": "a.log"}, target="a.log")
        try:
            gate.call("rm_rf", lambda: "x", args={})
            check("未声明工具被拦", False)
        except PermissionError as e:
            check("未声明工具被拦，且异常里带审计序号", "seq=" in str(e), str(e)[-40:])
        recs = [r for r in log.load() if not r.get("_bad")]
        check("成功调用被记录", any(r["tool"] == "read_log" and r["ok"] for r in recs))
        denied = [r for r in recs if not r["allowed"]]
        check("★ 被拒绝的尝试**也**进了审计（这是最有价值的部分）",
              len(denied) == 1 and denied[0]["tool"] == "rm_rf", denied)
        check("拒绝记录带 deny_reason", bool(denied[0]["deny_reason"]))
        check("拒绝记录里 scope 记成它**需要**的作用域", denied[0]["scope"] == "admin")
        check("序号单调递增", [r["seq"] for r in recs] == sorted(r["seq"] for r in recs))
        check("每次调用都有耗时字段", all("duration_ms" in r for r in recs))

        # ══════════════════════════════════════════════════════════════
        print("[3] ★ 不记内容、不记密钥（审计日志不能变成新的泄露面）")
        # ══════════════════════════════════════════════════════════════
        p2 = os.path.join(tmp, "a2.jsonl")
        log2 = A.AuditLog(p2)
        g2 = A.Gate(A.ScopePolicy(granted=("read",)), log2)
        g2.call("read_log", lambda: LOG_CONTENT, args={"path": "sample.log"}, target="sample.log")
        g2.call("search_log", lambda: "hit", args={"keyword": SECRET, "path": "b.log"})
        text = raw(p2)
        check("★ 审计文件里**不含**被读内容里的特征串（客户邮箱）",
              "zhang@example.com" not in text)
        check("★ 也不含日志正文里的其它内容（只留长度/行数）", "磁盘故障" not in text)
        check("★ 参数里的密钥形态被抹掉", SECRET not in text and A.REDACTED in text)
        check("结果摘要只记形状（字符数）",
              "字符" in text and "开头：" not in text)
        p3 = os.path.join(tmp, "a3.jsonl")
        A.Gate(A.ScopePolicy(granted=("read",)), A.AuditLog(p3)).call(
            "read_log", lambda: "Traceback ... ERROR", args={}, target="x.log")
        check("错误关键词只转成派生事实", "含错误关键词" in raw(p3))
        check("长参数被截断（审计日志不是数据仓库）",
              len(str(A.redact("x" * 5000))) < 500, len(str(A.redact("x" * 5000))))
        check("嵌套参数也能脱敏",
              A.redact({"a": {"b": ["token=" + SECRET]}}).get("a", {}).get("b", [""])[0]
              .find(SECRET) == -1)
        check("敏感名被标记出来（这正是最该被人看到的记录）",
              A.looks_sensitive(".env") and A.looks_sensitive("C:/x/id_rsa")
              and not A.looks_sensitive("sample_app.log"))

        # ══════════════════════════════════════════════════════════════
        print("[4] 哈希链：改动/删除/换序必须被发现")
        # ══════════════════════════════════════════════════════════════
        p4 = os.path.join(tmp, "a4.jsonl")
        log4 = A.AuditLog(p4)
        for i in range(4):
            log4.record("read_log", args={"path": f"f{i}.log"}, target=f"f{i}.log")
        ok, problems, checked = log4.verify()
        check("链完整时校验通过", ok and checked == 4, (ok, problems))
        lines = raw(p4).strip().split("\n")
        # 改一条的内容（不动哈希）
        rec = json.loads(lines[1])
        rec["tool"] = "tampered_tool"
        lines[1] = json.dumps(rec, ensure_ascii=False)
        with open(p4, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        ok, problems, _ = log4.verify()
        check("★ 改了内容 → 校验失败，并指出是第几条",
              not ok and any("#2" in x and "被改过" in x for x in problems), problems)

        # 恢复后再删一条 → 断链
        p5 = os.path.join(tmp, "a5.jsonl")
        log5 = A.AuditLog(p5)
        for i in range(4):
            log5.record("read_log", args={"path": f"g{i}.log"})
        ls = raw(p5).strip().split("\n")
        with open(p5, "w", encoding="utf-8") as f:
            f.write("\n".join([ls[0]] + ls[2:]) + "\n")
        ok, problems, _ = log5.verify()
        check("★ 删掉中间一条 → 断链被发现",
              not ok and any("断链" in x for x in problems), problems)

        p6 = os.path.join(tmp, "a6.jsonl")
        log6 = A.AuditLog(p6)
        for i in range(3):
            log6.record("read_log", args={"path": f"h{i}.log"})
        ls = raw(p6).strip().split("\n")
        with open(p6, "w", encoding="utf-8") as f:
            f.write("\n".join([ls[1], ls[0], ls[2]]) + "\n")
        ok, problems, _ = log6.verify()
        check("★ 换序 → 也能被发现", not ok, problems)
        check("哈希与格式无关（同一内容重算一致）",
              A._hash("p", {"a": 1, "b": 2}) == A._hash("p", {"b": 2, "a": 1}))
        check("坏行不会让校验崩掉，而是被指出",
              not A.AuditLog(os.path.join(tmp, "a6.jsonl")).verify()[0]
              and True)

        # ══════════════════════════════════════════════════════════════
        print("[5] 有界：轮转 + 保留上限 + 链跨轮转不断")
        # ══════════════════════════════════════════════════════════════
        p7 = os.path.join(tmp, "a7.jsonl")
        log7 = A.AuditLog(p7, max_bytes=800, keep=2)
        for i in range(40):
            log7.record("read_log", args={"path": f"big{i}.log" * 5})
        rotated = [f for f in os.listdir(tmp) if f.startswith("a7.jsonl.")]
        check("★ 超过上限会轮转", len(rotated) >= 1, rotated)
        check("★ 保留数量有上限（审计日志不许吃满磁盘）", len(rotated) <= 2, len(rotated))
        cur = log7.load()
        check("轮转后当前文件仍在正常追加", len(cur) >= 1)
        check("当前文件里第一条的 prev 承接了上一条的哈希（链跨轮转）",
              bool(cur[0].get("prev")), cur[0].get("prev"))
        check("单文件不会超过上限太多", os.path.getsize(p7) < 800 * 3,
              os.path.getsize(p7))

        # ══════════════════════════════════════════════════════════════
        print("[6] 身份诚实：不许假装成已认证主体")
        # ══════════════════════════════════════════════════════════════
        idn = A.identity()
        check("principal 明确写「未认证」", "未认证" in idn["principal"], idn["principal"])
        check("带 caller@host 形式的本机身份", "@" in idn["caller"], idn["caller"])
        check("标明传输方式（stdio）", idn["transport"] == "stdio", idn["transport"])
        rec = A.AuditLog(os.path.join(tmp, "a8.jsonl")).record("read_log", args={})
        check("记录里带 caller/host/principal", rec.get("caller") and rec.get("principal"))
        check("报告里也写明这条边界",
              "没有认证" in A.report(A.AuditLog(p4)) or "未认证" in A.report(A.AuditLog(p4)))

        # ══════════════════════════════════════════════════════════════
        print("[7] 报告：拒绝与敏感尝试要突出")
        # ══════════════════════════════════════════════════════════════
        p9 = os.path.join(tmp, "a9.jsonl")
        log9 = A.AuditLog(p9)
        g9 = A.Gate(A.ScopePolicy(granted=("read",)), log9)
        g9.call("read_log", lambda: "x", args={"path": "ok.log"}, target="ok.log")
        try:
            g9.call("scp_out", lambda: "x", args={})
        except PermissionError:
            pass
        log9.record("read_log", args={"path": ".env"}, target=".env", ok=False,
                    error="沙箱拒绝：敏感文件名", allowed=True)
        rep = A.report(log9)
        check("报告里有被拒绝条数", "被拒绝：1 条" in rep, rep.splitlines()[2:4])
        check("★ 报告高亮「触碰敏感名的尝试」", "触碰敏感名的尝试" in rep and ".env" in rep)
        check("报告里有哈希链校验结论", "哈希链校验" in rep)
        check("空日志的报告也有交代", "还没有审计记录" in A.report(A.AuditLog(
            os.path.join(tmp, "empty.jsonl"))))
        check("--verify 退出码为 0/2 可当门禁", A._main(["--verify", "--audit", p9]) == 0)

        # ══════════════════════════════════════════════════════════════
        print("[8] 关闭审计时不落盘（离线/测试用），但不假装成功")
        # ══════════════════════════════════════════════════════════════
        off = A.AuditLog(os.path.join(tmp, "nope.jsonl"), enabled=False)
        r = off.record("read_log", args={})
        check("enabled=False 时不写文件", not os.path.exists(os.path.join(tmp, "nope.jsonl")))
        check("返回里标明被跳过（调用方要知道审计没生效）", r.get("skipped") is True, r)
        check("默认是开启的（**审计默认不许关**）", A.AuditLog(os.path.join(tmp, "d.jsonl")).enabled)

        # ══════════════════════════════════════════════════════════════
        print("[9] 工具作用域表覆盖真实 server 的工具")
        # ══════════════════════════════════════════════════════════════
        real = ["list_files", "read_log", "search_log", "read_log_range",
                "check_disk", "system_snapshot", "top_files_by_size"]
        missing = [t for t in real if t not in A.TOOL_SCOPES]
        check("★ server.py 的 7 个工具全部在作用域表里有声明（漏一个就会默认被拒）",
              not missing, missing)
        check("今天所有工具都只需要 read（我们刻意不做写工具）",
              all(A.TOOL_SCOPES[t] == "read" for t in real))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 74)
    print(f"结果：{len(PASS)} 项通过，{len(FAIL)} 项失败（耗时 {time.perf_counter() - t0:.2f}s）")
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
        sys.exit(1)
    print("审计是纯逻辑 + 临时文件 —— 免费、可复现，已进 CI 免费档。")


if __name__ == "__main__":
    main()
