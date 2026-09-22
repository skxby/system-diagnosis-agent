#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""作品 3 · 上下文工程 —— context.py

**核心前提（实测校准，不是猜的）**：这份 3672 行 / 286 KB 的真实日志 ≈ **121k token**。
整份塞进 prompt 是不可能的。上下文工程就是回答"在有限预算里，塞什么进去最值"。

四种策略（对应 Anthropic《Effective context engineering》那三类 + 一个预算器）：

    ① 预算感知装填 fit_snippets()  —— 按价值挑选 + **明确告诉模型省了什么**
    ② 压缩         compact_text()   —— 分块摘要再递进合并（多花调用换更小的上下文）
    ③ 外部记忆     NoteStore        —— 结论落盘，下一轮只注入相关笔记（不重复喂原文）
    ④ 子 agent     run_subagent()   —— 独立上下文干活，**只回传结论**（上下文隔离）

═══════════════════════════════════════════════════════════════════
token 估算：**对着真实 API 校准过**（2026-09-22，deepseek-flash 实测）

    英文散文        5.80 字符/token
    中文散文        1.67
    JSON/日志（英文）2.55
    日志（中英混）   1.79
    真实日志前 2000 行 2.43      ← 日志场景就用这一档

估算器**故意保守**（把 token 数估高一点）：估高了会早一点截断，估低了会真的超上下文。
决策用估算器，**报告里的 token 一律用 API 返回的真实 usage** —— 两者不要混。
"""
import os
import re
import json
import time
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))
NOTES_PATH = os.path.join(HERE, "memory", "notes.jsonl")

# 校准值（字符/token）。取偏小的一侧 = 估出更多 token = 更保守
CJK_CHARS_PER_TOKEN = 1.6
OTHER_CHARS_PER_TOKEN = 2.4


def estimate_tokens(text):
    """估算 token 数（偏保守）。中文字符与其它字符分开算 —— 两者差 3 倍以上。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    rest = len(text) - cjk
    return int(cjk / CJK_CHARS_PER_TOKEN + rest / OTHER_CHARS_PER_TOKEN) + 1


def make_llm(client, model, usage_sink=None, temperature=0.0, label="ctx",
             max_tokens=2000, autoscale_empty=True):
    """把 OpenAI 客户端包成 `llm(system, user) -> str`，并把 **API 返回的真实 usage** 回调出去。

    为什么单独做这一层：
      · context.py 不依赖任何 SDK（好测、好换后端）—— 这跟 Day 30 的"LLM 插座"是同一个模式
      · 估算器只用来**做决策**（要不要截断、装多少），**报告里的 token 必须用真实 usage** ——
        两者混用会让你以为省了 30%，实际只省了 8%

    ⚠️ 两条实测教训都写进了这里：

    ① **输出必须有上限**：没限制时"压缩器"13 个块生成了 14 万 token、单次跑 590 秒 ——
       帮你省上下文的组件自己变成了成本黑洞。

    ② **但上限太小会得到"空正文"**（本机 deepseek-flash 实测，同一个提示词）：

          max_tokens=200 → finish=length，正文 0 字，推理 332 字
          max_tokens=400 → finish=length，正文 0 字，推理 701 字
          max_tokens=800 → finish=stop，  正文正常

       推理内容**先**吃预算，正文被挤没；而且它不报错，只是 `finish_reason=length`。
       所以这里加了**空正文守卫**：正文为空就放大一档重试（推理型模型的必踩坑）。
    """
    def _llm(system, user, max_tokens_override=None):
        base = max_tokens_override or max_tokens
        caps = [base, base * 2, base * 3] if autoscale_empty else [base]
        for attempt, cap in enumerate(caps, 1):
            r = client.chat.completions.create(
                model=model, temperature=temperature, max_tokens=cap,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}])
            choice = r.choices[0]
            text = choice.message.content or ""
            reasoning = getattr(choice.message, "reasoning_content", None) or ""
            if usage_sink is not None:
                u = getattr(r, "usage", None)
                usage_sink.append({
                    "label": label, "attempt": attempt, "max_tokens": cap,
                    "prompt_tokens": getattr(u, "prompt_tokens", 0) or 0,
                    "completion_tokens": getattr(u, "completion_tokens", 0) or 0,
                    "prompt_chars": len(system) + len(user),
                    "content_chars": len(text), "reasoning_chars": len(reasoning),
                    "finish_reason": choice.finish_reason,
                })
            if text.strip():
                return text
            print(f"    ⚠️ [{label}] 正文为空（finish={choice.finish_reason}、"
                  f"推理 {len(reasoning)} 字、max_tokens={cap}）→ 放大一档重试")
        raise RuntimeError(
            f"[{label}] 连续 {len(caps)} 次拿到空正文 —— 典型的'推理吃光输出预算'；"
            f"请提高 max_tokens 或缩短提示词（**不要静默当成空答案往下走**）")
    return _llm


def _clip_lines(text, max_chars, keep_tail=True):
    """按行截断，保留头+尾（日志的故障往往在尾部爆发，但头部有起点信息）。"""
    if len(text) <= max_chars:
        return text, 0
    lines = text.splitlines()
    half = max(1, max_chars // 2)
    head, tail, used, dropped = [], [], 0, 0
    for ln in lines:
        if used + len(ln) + 1 > half:
            break
        head.append(ln)
        used += len(ln) + 1
    used = 0
    for ln in reversed(lines):
        if used + len(ln) + 1 > half:
            break
        tail.append(ln)
        used += len(ln) + 1
    dropped = len(lines) - len(head) - len(tail)
    if not head and not tail:
        # 边界：**单行超长**（比如一整行 JSON / 一条堆栈）时按行切会两头都放不下 → 退化成空。
        # 兜底改成字符级截断（保留头尾），否则这段证据等于被静默删掉。
        half = max(1, max_chars // 2)
        body = f"{text[:half]}\n…（单行内容过长，中间省略 {max(0, len(text) - 2 * half)} 字符）…\n{text[-half:]}"
        return body, 1
    body = "\n".join(head + [f"…（此处省略 {dropped} 行）…"] + list(reversed(tail)))
    return body, dropped


# ══════════════════════════════════════════════════════════════════
# ① 预算感知装填
# ══════════════════════════════════════════════════════════════════
@dataclass
class Snippet:
    """一段待装填的证据。score 越高越先装（检索分/相关性分/人工置顶都可以）。"""
    source: str
    text: str
    score: float = 0.0

    @property
    def tokens(self):
        return estimate_tokens(self.text)


def fit_snippets(snippets, max_tokens, header="", per_snippet_min_tokens=30):
    """把证据装进预算里，**并明确告诉模型省了什么**。

    为什么要标记省略（而不是悄悄砍掉）：模型看不到的部分它会**默认自己看全了** ——
    于是措辞会变得过度自信。Day 32 那条"截断必须告诉模型"在这里是同一个道理，
    只是从工具结果层上升到了上下文层。
    """
    budget = max_tokens - estimate_tokens(header)
    packed, stats = [], {"budget": max_tokens, "included": 0, "truncated": 0,
                         "dropped": 0, "dropped_lines": 0, "used_tokens": 0}
    for sn in sorted(snippets, key=lambda s: -s.score):
        if budget <= 0:
            stats["dropped"] += 1
            continue
        t = sn.tokens
        if t <= budget:
            packed.append((sn, sn.text))
            budget -= t
            stats["included"] += 1
        else:
            room_chars = int(budget * OTHER_CHARS_PER_TOKEN)
            if room_chars < per_snippet_min_tokens * OTHER_CHARS_PER_TOKEN:
                stats["dropped"] += 1
                continue
            body, dropped_lines = _clip_lines(sn.text, room_chars)
            packed.append((sn, body))
            budget -= estimate_tokens(body)
            stats["truncated"] += 1
            stats["dropped_lines"] += dropped_lines

    parts = []
    if header:
        parts.append(header)
    for sn, text in packed:
        parts.append(f"【{sn.source}】\n{text}")
    if stats["dropped"] or stats["truncated"]:
        parts.append(
            f"\n（上下文预算 {max_tokens} token：完整纳入 {stats['included']} 段，"
            f"截断 {stats['truncated']} 段（省掉 {stats['dropped_lines']} 行），"
            f"整段丢弃 {stats['dropped']} 段。**未纳入的部分你并没有看到** —— "
            f"若结论依赖它们，请显式说明不确定，不要当作已确认。）")
    body = "\n\n".join(parts)
    stats["used_tokens"] = estimate_tokens(body)
    return body, stats


# ══════════════════════════════════════════════════════════════════
# ② 压缩（compaction）：分块摘要 → 递进合并
# ══════════════════════════════════════════════════════════════════
COMPACT_SYS = ("你是日志压缩器。**只保留事实**：时间、主机/端口/路径、错误码、数量、趋势方向。"
               "不要推测根因，不要评价，不要写建议。用短句列表，每条一行。"
               "数字和标识符（如 10.20.31.7:5432、2048）必须原样保留。"
               "**硬性长度限制：最多 8 条，总共不超过 200 字**，超出就合并同类项。"
               "绝不复述原文行。")


def compact_text(text, llm, block_tokens=1500, target_tokens=600, max_levels=2,
                 label="compact", summary_max_tokens=6000):
    """把大段原文压成事实摘要。**多花调用，换更小的上下文**。

    两级：① 分块压缩 ② 若结果仍超目标，把各块摘要再合并压缩一次（递进）。
    返回 (摘要, stats)：stats 里的真实 token 用量由调用方从 usage 里填（这里只统计估算）。

    ⚠️ 实测（2026-09-22）：**压缩并不是免费的**。这份日志上 13 个块、
       每块都要模型"想清楚保留什么"（推理 3-4k 字），总消耗比原文还大。
       所以 `summary_max_tokens` 给到 6000 —— 推理型模型的推理**也算在 max_tokens 里**，
       给小了就会拿到空正文（本文件的空正文守卫会拦住，但会白花一次调用）。
    """
    t0 = time.perf_counter()
    stats = {"calls": 0, "blocks": 0, "levels": 1,
             "input_tokens_est": estimate_tokens(text), "output_tokens_est": 0}

    def summarize(chunk_text):
        stats["calls"] += 1
        return llm(COMPACT_SYS, chunk_text, max_tokens_override=summary_max_tokens)

    # ① 按 block_tokens 分块
    lines, blocks, cur = text.splitlines(), [], []
    cur_tokens = 0
    for ln in lines:
        t = estimate_tokens(ln)
        if cur_tokens + t > block_tokens and cur:
            blocks.append("\n".join(cur))
            cur, cur_tokens = [], 0
        cur.append(ln)
        cur_tokens += t
    if cur:
        blocks.append("\n".join(cur))
    stats["blocks"] = len(blocks)
    summaries = [summarize(b) for b in blocks]

    # ② 递进：还超目标就把摘要再压一层
    merged = "\n".join(summaries)
    level = 1
    while estimate_tokens(merged) > target_tokens and level < max_levels:
        level += 1
        stats["levels"] = level
        groups, cur, cur_tokens = [], [], 0
        for s in summaries:
            t = estimate_tokens(s)
            if cur_tokens + t > block_tokens and cur:
                groups.append("\n".join(cur))
                cur, cur_tokens = [], 0
            cur.append(s)
            cur_tokens += t
        if cur:
            groups.append("\n".join(cur))
        summaries = [summarize(g) for g in groups]
        merged = "\n".join(summaries)

    stats["output_tokens_est"] = estimate_tokens(merged)
    stats["seconds"] = round(time.perf_counter() - t0, 1)
    return merged, stats


# ══════════════════════════════════════════════════════════════════
# ③ 外部记忆（note-taking）：结论落盘，下一轮只注入相关笔记
# ══════════════════════════════════════════════════════════════════
class NoteStore:
    """JSONL 落盘的结构化笔记，**按事件（incident）隔离**。

    为什么需要它：同一个事件往往要问好几次（"根因是什么" → "怎么处置" → "有回滚方案吗"）。
    每次都把原文重喂一遍 = 上下文滚雪球；把**上一轮的结论**记成笔记、按相关度召回，
    才叫"记住"而不是"重读"。

    ⚠️ 实测踩到的设计错：一开始没有 incident 维度，笔记跨轮累积、召回按词重叠取 top-k ——
       结果"磁盘"那条被别的笔记挤掉了，追问报告漏了一个故障域。
       真实语义是：**笔记属于某个事件；同一事件的追问应该把该事件的笔记全带上**，
       跨事件才需要按相关度筛。所以 add 必带 incident，recall 先按 incident 过滤。
    """

    def __init__(self, path=NOTES_PATH, incident="default"):
        self.path = path
        self.incident = incident
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def add(self, text, kind="finding", tags=None, source=""):
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind,
               "incident": self.incident, "text": text.strip(),
               "tags": tags or [], "source": source}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def all(self, incident=None):
        """默认只看**本事件**的笔记（跨事件混在一起正是上面那个 bug 的根源）"""
        want = incident if incident is not None else self.incident
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if want is None or rec.get("incident") == want:
                    out.append(rec)
        return out

    @staticmethod
    def _terms(text):
        """粗分词：ASCII 词 + 中文二字组（不引第三方分词器，够用且快）"""
        words = set(re.findall(r"[A-Za-z0-9_.:]{2,}", text.lower()))
        cjk = "".join(ch for ch in text if "\u4e00" <= ch <= "\u9fff")
        words |= {cjk[i:i + 2] for i in range(len(cjk) - 1)}
        return words

    def recall(self, query, k=5, incident=None):
        """本事件的笔记里按词重叠排序取前 k（incident 内不做 hard filter：
        同一事件的每条结论都相关，排序只是决定谁排前面）。"""
        q = self._terms(query)
        scored = []
        for rec in self.all(incident=incident):
            r = self._terms(rec["text"] + " " + " ".join(rec.get("tags", [])))
            scored.append((len(q & r), rec))
        scored.sort(key=lambda x: (-x[0], x[1]["ts"]))
        return [rec for _, rec in scored[:k]]

    def render(self, notes):
        if not notes:
            return ""
        lines = [f"- [{n['kind']}] {n['text']}" for n in notes]
        return "【历史笔记（同一事件的前几轮结论，按相关度召回）】\n" + "\n".join(lines)


# ══════════════════════════════════════════════════════════════════
# ④ 子 agent：独立上下文，只回传结论
# ══════════════════════════════════════════════════════════════════
SUBAGENT_SYS = ("你是取证子 agent。只看给你的证据，回答给定的子任务。"
                "**只输出 100 字以内的结论**；证据不足就直说不足，不要推测。")


def run_subagent(question, evidence_text, llm, max_evidence_tokens=3000, label="sub"):
    """上下文隔离的取证子 agent。

    契约（本文件最值钱的一条）：**父级上下文只增加"结论"，不增加"证据"。**
    证据 3000 token 进去，结论 100 token 出来 —— 这一进一出就是"子 agent 省上下文"的全部含义。
    实测这层隔离能省多少，看 stats 里的 evidence_tokens_est vs returned_tokens_est。
    """
    fitted, fit_stats = fit_snippets(
        [Snippet(source=label, text=evidence_text, score=1.0)],
        max_tokens=max_evidence_tokens)
    t0 = time.perf_counter()
    conclusion = llm(SUBAGENT_SYS, f"子任务：{question}\n\n证据：\n{fitted}")
    return conclusion, {
        "evidence_tokens_est": fit_stats["used_tokens"],
        "returned_tokens_est": estimate_tokens(conclusion),
        "truncated": fit_stats["truncated"],
        "dropped_lines": fit_stats["dropped_lines"],
        "seconds": round(time.perf_counter() - t0, 1),
    }
