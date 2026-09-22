#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
作品 3 · 第 9 周 Day 32 —— Function Calling 第一课：手写工具调用循环
════════════════════════════════════════════════════════════
本周要懂的唯一一件事，官方文档原话：

    "The model itself does not execute specific functions."

模型只会**输出**"我想调 search_log，参数是 {keyword: ERROR}"，
**执行是你的活**。所谓 Agent，就是把这个循环转起来：

    用户问题 → 模型要工具 → 你执行 → 结果回传 → 模型可能还要工具 → …… → 模型给最终回答

本文件已完整可跑。但**要理解的是判据，不是代码**：
    run_tool_loop() 里三个分支为什么这么判 —— 见该函数 docstring 的 ①②③

用法：
    python day32_function_calling.py --selftest        # 不开模型，验工具本身
    python day32_function_calling.py "系统有什么异常吗"    # 跑完整循环
"""
import os
import json
import sys
import shutil
import fnmatch

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())

MODEL = "deepseek-flash"   # 2026-09-21 核验：规范名，旧名 deepseek-v4-flash 已退休

# 工具只允许操作这个目录下的文件 —— 工具是 Agent 的攻击面，
# 模型给出的 path 是不可信输入（它可能被诱导去读 /etc/shadow）
#
# 注意这是**专门的数据目录**，不是脚本所在目录：脚本目录里躺着 .env（含 API key）
# 和脚本自身，把它当沙箱根，模型一次普通的 read_log(".env") 就能把密钥读进上下文，
# 不需要任何穿越技巧。真机部署时这个值应该来自配置（比如数据在 /var/log/app）。
ALLOWED_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")


# ══════════════════════════════════════════════════
# 三个工具的实现（读日志 / 搜日志 / 查磁盘）
# ══════════════════════════════════════════════════
def read_log(path, lines=20):
    """读日志文件最后 N 行"""
    n = int(lines)
    if n < 1:
        # 必须显式拦：all_lines[-(-1):] == all_lines[1:]，
        # lines=-1 会静默返回「丢掉第一行后的全文件」——不报错、结果错。
        # 这类静默错误比崩溃贵得多：循环照跑，报告看着正常，数据是错的。
        raise ValueError(f"lines 必须是正整数，收到 {lines!r}")
    full = _safe_path(path)
    with open(full, "r", encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()
    tail = all_lines[-n:]
    return "".join(f"{i + 1}: {t}" for i, t in enumerate(tail, start=len(all_lines) - len(tail)))


def search_log(path, keyword, max_hits=5):
    """在日志里搜关键词（大小写不敏感），返回命中行及行号"""
    full = _safe_path(path)
    with open(full, "r", encoding="utf-8", errors="replace") as f:
        hits = [(i, line.rstrip()) for i, line in enumerate(f, 1)
                if keyword.lower() in line.lower()]
    if not hits:
        return f"没找到包含「{keyword}」的行"
    shown = hits[:int(max_hits)]
    body = "\n".join(f"{i}: {t}" for i, t in shown)
    more = f"\n（共 {len(hits)} 处命中，只显示前 {len(shown)} 条）" if len(hits) > len(shown) else ""
    return body + more


def check_disk(path=None):
    """查磁盘占用。用 shutil 走跨平台，真机运维应该用 df -h —— 这里是为了能在 Windows 上练"""
    target = path or ALLOWED_ROOT
    usage = shutil.disk_usage(target)
    gb = 1024 ** 3
    return (f"{target}：总计 {usage.total / gb:.1f} GB，"
            f"已用 {usage.used / gb:.1f} GB，剩余 {usage.free / gb:.1f} GB"
            f"（{usage.used / usage.total * 100:.1f}% 已用）")


def list_files(pattern="*"):
    """列出目录下的文件。

    为什么必须有这个工具：**工具是 Agent 的眼睛**。
    不给它探索能力，它就只能"猜"文件名 —— 实测中它连猜 5 个都不中，
    最后只能回头问你。这不是模型笨，是工具设计漏了。
    """
    names = sorted(n for n in os.listdir(ALLOWED_ROOT)
                   if os.path.isfile(os.path.join(ALLOWED_ROOT, n)))
    hit = [n for n in names if fnmatch.fnmatch(n, pattern)]
    if not hit:
        return f"没有匹配 {pattern} 的文件。目录下现有：{', '.join(names)}"
    return "\n".join(hit)


def _safe_path(path):
    """把模型给的路径限制在 ALLOWED_ROOT 内，越界就抛错。

    别用 str.startswith 判父子 —— 那是**字符串前缀**匹配，不是路径语义：
    "作品3_诊断Agent_x" 以 "作品3_诊断Agent" 开头，会被它放过去（实测穿透过）。
    而且 startswith 大小写敏感，Windows 上 "E:\\AI学习\\..." 这种合法写法反被误拦。

    realpath 一步解决两件事：解析掉 .. 和符号链接（不解析的话，一个指向外部的
    软链接就能穿），并把大小写归一化成磁盘上的真实形式。
    再用 commonpath 做路径语义的父子判断。
    """
    root = os.path.realpath(ALLOWED_ROOT)
    full = os.path.realpath(os.path.join(root, path))
    try:
        inside = os.path.commonpath([full, root]) == root
    except ValueError:
        # 不同盘符时 commonpath 直接抛 —— 那必然是越界。
        # 这里必须 catch：否则模型给个异地盘符路径会撞成崩溃，而不是被拒绝。
        inside = False
    if not inside:
        raise ValueError(f"路径越界，拒绝访问：{path}")
    return full


# ══════════════════════════════════════════════════
# 工具的 JSON Schema —— 给模型看的"说明书"
# ══════════════════════════════════════════════════
# description 不是给人看的注释，是模型选工具的唯一依据 —— 它是 prompt 的一部分
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出数据目录下的文件，支持通配符（如 *.log）。仅在用户要求排查系统问题时调用，用于确认有哪些日志文件可读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "通配符，默认 * 表示全部"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_log",
            "description": "读取日志文件最后若干行。适合在没有明确关键词时先看日志尾部概况。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "日志文件路径（相对数据目录）"},
                    "lines": {"type": "integer", "description": "读取行数，默认 20"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_log",
            "description": "在日志文件里按关键词搜索，返回命中行和行号。已知要找什么（如 ERROR）时优先用它。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "日志文件路径（相对数据目录）"},
                    "keyword": {"type": "string", "description": "要搜索的关键词，如 ERROR、磁盘"},
                    "max_hits": {"type": "integer", "description": "最多返回几条，默认 5"},
                },
                "required": ["path", "keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_disk",
            "description": "查看磁盘空间占用情况。怀疑空间不足时使用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    
]

TOOL_IMPL = {"list_files": list_files, "read_log": read_log,
             "search_log": search_log, "check_disk": check_disk}


# ══════════════════════════════════════════════════
# 工具执行器：解析参数 → 执行 → 把结果转成字符串回传
# ══════════════════════════════════════════════════
MAX_RESULT_CHARS = 4000   # 工具结果回传上限


def _clip(text, limit=MAX_RESULT_CHARS):
    """截断过长的工具结果。

    为什么必须截：① 结果要进 messages，下一轮整段历史都会重发，上下文是滚雪球；
                  ② 日志文件动辄几万行，一次全塞回去直接撑爆上下文窗口。
    截了要**告诉模型被截了**，否则它会以为看全了。
    """
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（结果被截断，原始长度 {len(text)} 字符）"


def execute_tool(tool_call):
    """把模型要的 tool call 真正执行掉，返回结果字符串。

    注意 arguments 是 **JSON 字符串**，不是 dict —— 模型输出的是文本，得自己 json.loads。
    工具执行失败不能抛出去，要把错误当结果喂回模型 —— 模型才有机会改参数重试。
    """
    name = tool_call.function.name
    raw_args = tool_call.function.arguments or "{}"
    print(f"    ↳ 调用 {name}({raw_args})")

    try:
        args = json.loads(raw_args)
    except json.JSONDecodeError as e:
        return f"参数不是合法 JSON：{e}"

    fn = TOOL_IMPL.get(name)
    if fn is None:
        return f"没有名为 {name} 的工具"
    try:
        return _clip(str(fn(**args)))
    except Exception as e:
        return f"工具执行失败（{type(e).__name__}）：{e}"


# ══════════════════════════════════════════════════
# 工具调用循环
# ══════════════════════════════════════════════════
def run_tool_loop(client, question, max_rounds=5):
    """把「模型要工具 → 你执行 → 结果回传」这个循环转起来，直到模型给出最终回答。

    三个分支判据是本函数的全部要害（比代码本身重要）：

      ① 判据是 **tool_calls 的存在性**，不是 content 的满空。
         模型可能同时返回 content 和 tool_calls（先写一句"让我查一下日志"再调工具），
         你若按"content 非空 = 最终答案"，就会把前言当成答案返回。

      ② finish_reason 有 5 个取值，不是 2 个：
         stop / length / tool_calls / insufficient_system_resource / aborted
         只判 tool_calls 的漏洞：length（截断）和 aborted（中断）会被当成正常答案返回 ——
         一条被截断或压根没生成完的回复，静默地交给用户，比报错难查得多。

      ③ 一次可能返回多个 tool_call（tool_choice=auto 原文是 "one or more tools"），
         只处理 tool_calls[0] 会凭空丢失其余结果。
    """
    messages = [{"role": "user", "content": question}]

    for round_no in range(1, max_rounds + 1):
        resp = client.chat.completions.create(
            model=MODEL, messages=messages, tools=TOOLS,
        )
        choice = resp.choices[0]
        msg = choice.message
        reason = choice.finish_reason
        n_calls = len(msg.tool_calls or [])
        print(f"  [第 {round_no} 轮] finish_reason={reason}  工具调用 {n_calls} 个")

        # ── ① 优先看 tool_calls 存不存在 ─────────────────
        if msg.tool_calls:
            if msg.content:          # 前言 + 工具调用同时出现，这个 case 就是坑表那一行的实证
                print(f"    （模型先说了：{msg.content.strip()[:40]}…）")
            messages.append(msg)     # 助手的"要工具"也是对话历史的一部分，漏了下一轮它会 400
            for tc in msg.tool_calls:            # ② 全部处理，不是 [0]
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": execute_tool(tc),
                })
            continue

        # ── ② 截断 / 服务端未完成：都不是最终答案 ──────────
        if reason == "length":
            return ("[生成被截断] 超过 max_tokens 或上下文上限，答案不完整。\n"
                    f"已生成部分：\n{msg.content or '（无）'}")
        if reason in ("insufficient_system_resource", "aborted"):
            return f"[服务端未完成：{reason}] 应当重试，不应把这次结果当答案。"

        # ── ③ stop：正常最终答案 ──────────────────────
        return msg.content or "（模型返回了空内容）"

    return (f"[超过 {max_rounds} 轮仍未结束] 模型可能陷入工具循环，"
            f"已执行 {max_rounds} 轮，请检查工具描述是否让模型无法收敛。")


# ══════════════════════════════════════════════════
# 客户端
# ══════════════════════════════════════════════════
def make_client():
    import httpx
    from openai import OpenAI
    return OpenAI(
        base_url="https://api.deepseek.com",
        api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
        http_client=httpx.Client(trust_env=False, timeout=120),
    )


# ══════════════════════════════════════════════════
# 自检：不开模型，直接验工具本身对不对（写循环前先跑这个）
# ══════════════════════════════════════════════════
def _selftest():
    print("=" * 60)
    print("工具自检（不调用模型）")
    print("=" * 60)
    print("\n[1] search_log('sample_app.log', 'ERROR')")
    print(search_log("sample_app.log", "ERROR"))
    print("\n[2] read_log('sample_app.log', 3)")
    print(read_log("sample_app.log", 3))
    print("\n[3] check_disk()")
    print(check_disk())
    print("\n[4] 路径防护：越界必须拦、合法必须放")
    for p in ("../../../Windows/win.ini",         # 上一级穿越
              "../作品3_诊断Agent_x/secret.txt",   # 兄弟目录前缀（曾穿透）
              "../.env"):                          # 脚本目录里的密钥，被隔离在沙箱外
        try:
            _safe_path(p)
            print(f"    ⚠️  没拦住 —— 防护失效：{p}")
        except ValueError as e:
            print(f"    ✅ {e}")
    for p in ("sample_app.log", os.path.join(ALLOWED_ROOT.upper(), "sample_app.log")):
        try:
            _safe_path(p)
            print(f"    ✅ 放行：{p}")
        except ValueError as e:
            print(f"    ⚠️  误拦合法路径：{e}")
    print("\n[5] 参数不是 JSON 时 execute_tool 的表现")
    class _FakeCall:
        class function:
            name = "check_disk"
            arguments = "{'path': 不是json}"
    print(execute_tool(_FakeCall))
    print("\n[6] list_files('*.log')")
    print(list_files("*.log"))


def main():
    # 自检不碰模型，所以放在 API key 检查之前 —— 先确认工具能用，再谈循环
    if "--selftest" in sys.argv:
        _selftest()
        return
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("错误：.env 里没有 DEEPSEEK_API_KEY")
        return

    question = next((a for a in sys.argv[1:] if not a.startswith("--")), None)
    if question is None:
        # 用法提示给绝对路径：从任何目录都能直接粘贴执行，
        # 避免"我脚本明明存在，python 却说找不到"（相对路径按工作目录解析）
        print("用法：")
        print(f'  python "{os.path.abspath(__file__)}" "系统有什么异常吗"')
        print("  （上面是绝对路径，在哪个目录下都能跑）")
        return

    client = make_client()
    try:
        print(run_tool_loop(client, question))
    except NotImplementedError as e:
        print(f"\n{e}\n\n先用 --selftest 确认工具可用，再动手写循环。")


if __name__ == "__main__":
    main()
