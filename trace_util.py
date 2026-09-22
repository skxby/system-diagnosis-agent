#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""作品 3 · 追踪层（可替换插座）—— trace_util.py

同一套调用接口，两个后端：

    JsonlTracer      写本地 traces/*.jsonl —— 永远可用，能离线跑、能 diff、能进 git
    LangfuseTracer   上报 Langfuse（OTEL）—— **只在配了 key 时启用**

为什么做成插座（这是本作品第三次用同一个模式）：
    Day 30  LLM 插座    （callable(messages)->str，换后端不改上层）
    Day 36  工具插座    （DirectTools / McpTools，换通道不改图）
    Day 37  追踪插座    （本地 JSONL / Langfuse，换观测后端不改节点）

⚠️ 实测教训（langfuse 4.15.4）：**没有服务端时不要启用 Langfuse 后端** ——
   它不会报错，但退出时会 "Failed to export span batch due to timeout, max retries or shutdown"，
   白白等超时。所以 make_tracer() 只在真的有 key 时才选它。

Langfuse v4 的接口（2026-09 本机实测，注意 v3 的 start_as_current_span 已经不存在了）：
    langfuse.start_as_current_observation(name=..., as_type="span"|"tool"|"generation"|"agent"|...)
    langfuse.update_current_span(output=..., metadata=...)
    langfuse.create_event(name=..., metadata=...)      # 必须在某个 span 上下文里
    langfuse.get_trace_url(trace_id=...)
"""
import os
import json
import time
import uuid
import contextvars
from contextlib import contextmanager

TRACE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "traces")


# ══════════════════════════════════════════════════════════════════
# 后端 A：本地 JSONL（默认，永远可用）
# ══════════════════════════════════════════════════════════════════
class JsonlTracer:
    """每行一条记录，append-only。为什么要 JSONL 而不是 logging：

      · 一行一条 → 可以 tail、可以 jq、可以进 git diff
      · 结构化的字段 → 能写脚本算指标（本文件的 report() 就是）
      · **跨进程天然能接上**：谁写的都往同一个 trace 文件追加，靠 run/thread 字段归组
    """

    backend = "jsonl"

    def __init__(self, thread_id, run_id, path=None):
        os.makedirs(TRACE_DIR, exist_ok=True)
        self.thread_id = thread_id
        self.run_id = run_id
        self.path = path or os.path.join(TRACE_DIR, f"{thread_id}.jsonl")
        # ⚠️ span 栈必须是**上下文局部**的（contextvars），不能是普通 list / 全局变量：
        #    实测踩过 —— 4 个并行 investigate 分支共用一个 list，push/pop 交错，
        #    结果 trace 里它们互相嵌套成了"一条链"，parent 全错。
        #    contextvars 在 asyncio 里每个任务拿到的是**自己的副本**，兄弟分支互不干扰。
        #    （线程池场景同理要用 threading.local —— 一句话：**并发下的"当前状态"必须隔离**。）
        self._stack = contextvars.ContextVar(f"span_stack_{id(self)}", default=())

    def _write(self, record):
        record.update({"ts": time.time(), "trace": self.thread_id, "run": self.run_id})
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    @contextmanager
    def span(self, name, **attrs):
        span_id = uuid.uuid4().hex[:8]
        stack = self._stack.get()
        parent = stack[-1] if stack else None
        depth = len(stack)
        self._write({"type": "start", "name": name, "span": span_id,
                     "parent": parent, "depth": depth, "attrs": attrs})
        token = self._stack.set(stack + (span_id,))
        t0 = time.perf_counter()
        try:
            yield SpanHandle(self, span_id, attrs)
        finally:
            self._stack.reset(token)
            self._write({"type": "end", "name": name, "span": span_id, "parent": parent,
                         "depth": depth, "attrs": attrs, "dur": time.perf_counter() - t0})

    def event(self, name, **attrs):
        stack = self._stack.get()
        self._write({"type": "event", "name": name,
                     "span": stack[-1] if stack else None,
                     "depth": len(stack), "attrs": attrs})

    def flush(self):
        pass

    def trace_url(self):
        return None


class SpanHandle:
    """给调用方补属性的句柄（span 结束时统一落盘）"""

    def __init__(self, tracer, span_id, attrs):
        self._tracer, self.span_id, self.attrs = tracer, span_id, attrs

    def set(self, **attrs):
        self.attrs.update(attrs)


# ══════════════════════════════════════════════════════════════════
# 后端 B：Langfuse（只在有 key 时用）
# ══════════════════════════════════════════════════════════════════
class LangfuseTracer:
    """把同一套 span/event 调用翻译成 Langfuse 的 OTEL 接口。

    启用条件（三者缺一不可）：装了 langfuse 包 + 配了 PUBLIC/SECRET key。
    没有服务端时**别进这里** —— 见文件头的实测教训。
    """

    backend = "langfuse"

    def __init__(self, thread_id, run_id, host=None):
        from langfuse import Langfuse
        self.client = Langfuse(host=host or os.environ.get("LANGFUSE_HOST") or "https://cloud.langfuse.com")
        self.thread_id = thread_id
        self.run_id = run_id
        self._ctx = []

    @contextmanager
    def span(self, name, as_type="span", **attrs):
        with self.client.start_as_current_observation(
                name=name, as_type=as_type,
                input=attrs.pop("input", None),
                metadata={"run_id": self.run_id, **attrs}) as sp:
            handle = SpanHandle(None, None, attrs)
            try:
                yield handle
            finally:
                self.client.update_current_span(output=handle.attrs.pop("output", None),
                                                metadata={"run_id": self.run_id, **handle.attrs})

    def event(self, name, **attrs):
        try:
            self.client.create_event(name=name, metadata={"run_id": self.run_id, **attrs})
        except Exception:
            pass          # 观测层永远不能影响主流程

    def flush(self):
        try:
            self.client.flush()
        except Exception:
            pass

    def trace_url(self):
        try:
            return self.client.get_trace_url()
        except Exception:
            return None


class NullTracer:
    """关掉追踪时用它 —— 调用点代码一个字不用改（插座模式的标准收尾）"""

    backend = "none"
    path = None

    def __init__(self, thread_id=None, run_id=None):
        self.thread_id, self.run_id = thread_id, run_id

    @contextmanager
    def span(self, name, **attrs):
        yield SpanHandle(self, None, attrs)

    def event(self, name, **attrs):
        pass

    def flush(self):
        pass

    def trace_url(self):
        return None


# ══════════════════════════════════════════════════════════════════
# 门面：选后端 + 包装节点
# ══════════════════════════════════════════════════════════════════
def make_tracer(thread_id, run_id, prefer_langfuse=True, path=None):
    """有 key 用 Langfuse，否则本地 JSONL。**绝不因为没有服务端就卡住**。"""
    has_keys = bool(os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"))
    if prefer_langfuse and has_keys:
        try:
            t = LangfuseTracer(thread_id, run_id)
            print(f"  [trace] 后端 = Langfuse（{os.environ.get('LANGFUSE_HOST', 'https://cloud.langfuse.com')}）")
            return t
        except Exception as e:
            print(f"  [trace] Langfuse 不可用（{type(e).__name__}: {e}），退回本地 JSONL")
    elif prefer_langfuse:
        print("  [trace] 没配 LANGFUSE_PUBLIC_KEY/SECRET_KEY → 用本地 JSONL"
              "（配好 key 后同一条命令就会上报，节点代码不用改）")
    return JsonlTracer(thread_id, run_id, path)


def traced_node(tracer, name, fn, attrs_fn=None):
    """把一个节点函数包成带 span 的版本 —— 包装放在 add_node 处，节点体一个字不用改。

    这就是"可观测性不该侵入业务逻辑"的具体做法：观测是**横切**的，所以用包装/AOP，
    而不是在业务函数里到处插埋点。
    """
    async def inner(state):
        attrs = attrs_fn(state) if attrs_fn else {}
        with tracer.span(name, **attrs):
            return await fn(state)
    return inner


# ══════════════════════════════════════════════════════════════════
# 读回：把 JSONL 变成一份「追踪报告」
# ══════════════════════════════════════════════════════════════════
def load(path):
    recs = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                recs.append(json.loads(line))
    return recs


def report(path, show_spans=True):
    """跨进程也能拼出完整链路 —— 这正是 checkpointer（管状态）做不到的那一半。"""
    recs = load(path)
    if not recs:
        print(f"（{path} 是空的）")
        return {}

    runs = {}
    for r in recs:
        runs.setdefault(r["run"], []).append(r)

    print("=" * 78)
    print(f"追踪报告 · trace = {recs[0]['trace']}   （文件 {os.path.basename(path)}）")
    print("=" * 78)
    print(f"记录 {len(recs)} 条，覆盖 **{len(runs)} 个进程 run**"
          f"（checkpointer 管状态、trace 管过程 —— 跨进程续跑也能拼成一条链）")
    for run_id, rs in runs.items():
        ends = [x for x in rs if x["type"] == "end"]
        t0, t1 = min(x["ts"] for x in rs), max(x["ts"] for x in rs)
        print(f"  · run {run_id}：{len(rs)} 条记录，span {len(ends)} 个，"
              f"时长 {t1 - t0:.1f}s")

    if show_spans:
        print("\n" + "-" * 78)
        print("span 树（并发分支互为兄弟；缩进 = 调用关系，末尾 = 耗时）")
        print("-" * 78)
        # 用 parent 关系重建调用树后再打印 —— 直接按时间顺序平铺输出时，
        # 并发分支的 start/end 会交错在一起，读不出层次（这是自己踩过一次的显示坑）。
        nodes, events = {}, {}
        for r in recs:
            if r["type"] == "start":
                nodes[r["span"]] = {"name": r["name"], "attrs": r["attrs"], "run": r["run"],
                                    "parent": r["parent"], "dur": None, "ts": r["ts"]}
            elif r["type"] == "end":
                if r["span"] in nodes:
                    nodes[r["span"]]["dur"] = r["dur"]
            else:
                events.setdefault(r.get("span"), []).append(r)

        children = {}
        for sid, n in nodes.items():
            children.setdefault(n["parent"], []).append(sid)
        for lst in children.values():
            lst.sort(key=lambda s: nodes[s]["ts"])

        def walk(sid, level):
            n = nodes[sid]
            shown = {k: v for k, v in n["attrs"].items() if k != "as_type"}
            extra = "  " + json.dumps(shown, ensure_ascii=False)[:70] if shown else ""
            dur = f"{n['dur']:.2f}s" if n["dur"] is not None else "（未结束）"
            print(f"{'  ' * level}┌ {n['name']}{extra}   {dur}   [run {n['run']}]")
            for e in events.get(sid, []):
                print(f"{'  ' * (level + 1)}· {e['name']}  "
                      f"{json.dumps(e['attrs'], ensure_ascii=False)[:90]}")
            for c in children.get(sid, []):
                walk(c, level + 1)

        for root in children.get(None, []):
            walk(root, 0)
        for e in events.get(None, []):
            print(f"· {e['name']}  {json.dumps(e['attrs'], ensure_ascii=False)[:90]}")

    llms = [r for r in recs if r["type"] == "end" and r["name"].startswith("llm:")]
    tools = [r for r in recs if r["type"] == "end" and r["name"].startswith("tool:")]
    others = [r for r in recs if r["type"] == "event"]

    print("\n" + "-" * 78)
    print("按类型统计")
    print("-" * 78)
    if llms:
        tok_in = sum(r["attrs"].get("tokens_in") or 0 for r in llms)
        tok_out = sum(r["attrs"].get("tokens_out") or 0 for r in llms)
        total = sum(r["dur"] for r in llms)
        by_node = {}
        for r in llms:
            by_node.setdefault(r["name"][4:], []).append(r["dur"])
        print(f"LLM：{len(llms)} 次，合计 {total:.1f}s，"
              f"token 输入 {tok_in} / 输出 {tok_out}（合计 {tok_in + tok_out}）")
        for node, ds in by_node.items():
            print(f"    · {node:<12} {len(ds)} 次  合计 {sum(ds):.1f}s  最慢 {max(ds):.1f}s")
    if tools:
        total = sum(r["dur"] for r in tools)
        chan = tools[0]["attrs"].get("channel")
        print(f"工具：{len(tools)} 次（通道 {chan}），合计 {total * 1000:.0f} ms，"
              f"平均 {total / len(tools) * 1000:.1f} ms/次")
        for r in tools:
            a = r["attrs"]
            print(f"    · {r['name'][5:]:<12} {r['dur'] * 1000:7.1f} ms  {str(a.get('args'))[:44]}")
    if others:
        print("其它事件：")
        for r in others:
            print(f"    · {r['name']:<12} {json.dumps(r['attrs'], ensure_ascii=False)[:100]}")

    if llms:
        slowest = max(llms, key=lambda r: r["dur"])
        print(f"\n最贵的 LLM 调用：{slowest['name'][4:]} {slowest['dur']:.1f}s"
              f"（通常由服务端排队/负载决定，不是你的代码问题）")
    print("\n读法：先看 span 树找**最长的那个盒子**，再看它是 LLM 还是工具 ——"
          "\n      优化要打在最长的盒子上，打错地方就是白干（Day 36 实测：工具通道 15.8ms vs LLM 数秒到两分钟）。")
    return {"runs": len(runs), "llm": len(llms), "tools": len(tools)}
