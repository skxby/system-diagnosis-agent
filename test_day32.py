#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Day 32 工具层 + 工具调用循环的回归测试。

分两层，别混：

  L1 离线层 —— 工具函数 / 路径校验 / 执行器。毫秒级、100% 确定、不需要 API key。
  L2 在线层 —— 模型选不选工具、循环跑不跑得完。秒级、花钱、非确定、需要 key。

**L2 失败不等于你的代码错**（模型是概率的），所以单独隔离：没 key 时整体跳过，
不让环境问题伪装成代码问题。

跑法（在 作品3_诊断Agent 目录下）：
  python -m pytest test_day32.py -v -k "not TestLoopOnline"   # 只跑离线，日常用
  python -m pytest test_day32.py -v                            # 全跑，含调模型

测试失败有两种，处理方式不同：

  · 代码坏了      → 断言直接失败，必须修。
  · 已知但暂不修  → @pytest.mark.xfail(strict=True) 登记在册，不卡绿。

xfail 的价值在于它**会自己过期**：普通 TODO 注释靠人记得回头删，
xfail 是代码自己提醒 —— 缺口修好后测试 XPASS，strict 立刻把它变红，
逼你摘掉标记。本项目的未处理项列在文件末尾。
"""
import os
import re
import sys
import types

import pytest
from dotenv import load_dotenv

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

# 显式按本文件位置加载 .env，不依赖 cwd ——
# 否则从别的目录跑 pytest 时 key 读不到，skipif 会把 L2 全跳过还看起来"正常"
load_dotenv(os.path.join(_HERE, ".env"))

import day32_function_calling as d32

LOG = "sample_app.log"


# ══════════════════════════════════════════════════
# 测试用的小工具
# ══════════════════════════════════════════════════
def _lines(text):
    """从工具输出里抽出 "12: xxx" 的行号，返回 int 列表。

    工具返回的是 "行号: 内容" 的人类可读文本，不是结构化数据 ——
    要断言就得先解析回来。这本身就是个设计信号：工具返回结构化 dict
    会更好测，也更好让模型引用。这里先按现状解析。
    """
    return [int(m) for m in re.findall(r"^(\d+):", text, flags=re.M)]


def _fake_call(name, arguments):
    """伪造一个 tool_call 对象。

    真实的 msg.tool_calls[i] 是 OpenAI SDK 的 pydantic 模型；
    execute_tool 只用得到 .function.name 和 .function.arguments 两个属性，
    所以 SimpleNamespace 就够 —— 这就是「依赖注入」最轻的形态，
    也是为什么 execute_tool 的入参是通用对象而不是具体类型。
    """
    func = types.SimpleNamespace(name=name, arguments=arguments)
    return types.SimpleNamespace(function=func)


# ══════════════════════════════════════════════════
# L1 离线层
# ══════════════════════════════════════════════════
class TestSafePath:
    """路径校验 —— 工具的安全边界。

    这里直接测私有函数 _safe_path。对「私有函数不该测」这条规则，
    安全边界是划算的例外：只通过 read_log 侧测会混进 FileNotFoundError
    等噪声，判不清到底哪一层拦的。测边界就测边界本身。
    """

    def test_reject_parent_traversal(self):
        """../../../Windows/win.ini —— 经典的上一级穿越"""
        with pytest.raises(ValueError):
            d32._safe_path("../../../Windows/win.ini")

    def test_reject_sibling_prefix_dir(self):
        """兄弟目录 ../作品3_诊断Agent_x/x 必须拒绝。

        陷阱在判据本身：用 str.startswith 判父子是**字符串前缀**匹配，
        "作品3_诊断Agent_x" 恰好以 "作品3_诊断Agent" 开头，就穿过去了
        （这个穿透实测复现过，本测试就是那条复现）。正解是路径语义比较。
        """
        evil = os.path.join("..", "作品3_诊断Agent_x", "secret.txt")
        with pytest.raises(ValueError):
            d32._safe_path(evil)

    def test_accept_uppercase_legal_path(self):
        """合法文件的大小写变体不该被拦。

        Windows 文件系统大小写不敏感，但 str.startswith 敏感 ——
        E:\\AI学习\\.VENV\\... 这种写法会被误判成越界。
        realpath 会把它归一化成磁盘上的真实形式再比较，所以不该抛。
        """
        legal = os.path.join(d32.ALLOWED_ROOT.upper(), LOG)
        d32._safe_path(legal)          # 不该抛

    def test_reject_secret_files(self):
        """密钥文件必须在沙箱之外 —— 沙箱根目录不能是脚本所在目录。

        脚本目录里躺着 .env（含 API key），把根设在那儿，模型一次普通的
        read_log(".env") 就能把密钥读进上下文，不需要任何穿越技巧。
        所以 ALLOWED_ROOT 指向专门的 logs/ 数据目录：密钥被隔离在沙箱外，
        模型无论怎么拼路径都够不到。
        """
        assert not os.path.exists(os.path.join(d32.ALLOWED_ROOT, ".env"))
        with pytest.raises(ValueError):
            d32._safe_path(os.path.join("..", ".env"))


class TestTools:
    """四个工具函数本身。基准值对着 sample_app.log（30 行）数出来的。"""

    def test_search_hits_with_lineno(self):
        """connection refused 在 fixture 里命中 2 处：第 6 行和第 24 行"""
        assert _lines(d32.search_log(LOG, "connection refused")) == [6, 24]

    def test_search_is_case_insensitive(self):
        """关键词大小写不敏感 —— 实现里两边都 lower 了，这条把契约钉住"""
        assert _lines(d32.search_log(LOG, "CONNECTION REFUSED")) == [6, 24]

    def test_search_no_hit_returns_text(self):
        """没命中返回提示文本，不抛异常 ——
        工具要能「回答没有」，而不是崩掉让循环处理异常。"""
        out = d32.search_log(LOG, "zzz_not_in_log")
        assert "没找到" in out
        assert _lines(out) == []

    def test_search_max_hits_truncates(self):
        """max_hits 限流，且必须告诉调用方「还有更多」——
        截了不说是静默错误，模型会以为看全了。"""
        out = d32.search_log(LOG, "ERROR", max_hits=1)
        assert len(_lines(out)) == 1
        assert "命中" in out           # 「共 N 处命中，只显示前 M 条」

    def test_read_log_tail(self):
        """lines=3 取尾部三行，行号应是 28/29/30（fixture 共 30 行）"""
        assert _lines(d32.read_log(LOG, 3)) == [28, 29, 30]

    def test_read_log_coerces_string_lines(self):
        """模型可能把数字传成字符串（JSON 里 "20" 和 20 都可能），
        实现用 int() 吞掉 —— 这条把「接受字符串数字」钉成契约。"""
        assert _lines(d32.read_log(LOG, "3")) == [28, 29, 30]

    def test_read_log_negative_lines_rejected(self):
        """lines 为负必须显式报错，绝不能静默。

        all_lines[-(-1):] == all_lines[1:] —— lines=-1 会返回
        「丢掉第一行后的全文件」，不报错、结果错。
        这类静默错误比崩溃贵得多：循环照跑、报告看着正常、数据是错的。

        本测试把契约定为「抛 ValueError」。若你更想要「夹紧到合法范围」，
        那就把这条改成断言返回值行数，而不是 raises ——
        两种契约都行，**静默是唯一不可接受的**。
        """
        with pytest.raises(ValueError):
            d32.read_log(LOG, -1)

    def test_list_files_glob(self):
        """通配符过滤生效"""
        assert d32.list_files("*.log").splitlines() == [LOG]

    def test_list_files_no_match_lists_dir(self):
        """没匹配时要告诉模型目录里有什么 ——
        这是「工具是 Agent 的眼睛」：不给探索能力，它只能猜文件名。"""
        assert LOG in d32.list_files("*.nope")


class TestClip:
    """_clip 是纯函数 —— 能抽成纯函数就抽，这是可测性设计：
    不用造一个超长日志文件，就能测截断逻辑。"""

    def test_short_text_untouched(self):
        assert d32._clip("短") == "短"

    def test_long_text_truncated_and_reports(self):
        """截断 + 报出原始长度。少了后半句，模型会以为看全了。"""
        out = d32._clip("x" * 5000)
        assert "结果被截断" in out
        assert "5000" in out
        assert len(out) < 5000


class TestExecuteTool:
    """执行器：把模型要的 tool call 变成结果字符串。
    核心契约是**不抛异常** —— 任何失败都当结果喂回模型。"""

    def test_bad_json_returns_error_not_raise(self):
        """arguments 不是合法 JSON —— 回传错误文本，让模型改参数重试。

        模型输出的 arguments 是**字符串**不是 dict，得自己 json.loads。
        """
        out = d32.execute_tool(_fake_call("check_disk", "{'path': 不是json}"))
        assert "不是合法 JSON" in out

    def test_empty_arguments_treated_as_empty_object(self):
        """arguments 可能是空串 —— 实现用 `or "{}"` 兜住，无参工具照常执行"""
        out = d32.execute_tool(_fake_call("check_disk", ""))
        assert "总计" in out           # 真跑到 check_disk 了
        assert "失败" not in out

    def test_unknown_tool_name(self):
        """工具名不在 registry —— 返回可读错误而不是 KeyError"""
        assert "没有名为 rm_rf 的工具" in d32.execute_tool(_fake_call("rm_rf", "{}"))

    def test_tool_exception_returned_as_text(self, monkeypatch):
        """工具内部抛异常 → 转成文本回传，循环不能当场崩"""
        def boom(**kwargs):
            raise ZeroDivisionError("故意炸")
        monkeypatch.setitem(d32.TOOL_IMPL, "boom", boom)

        out = d32.execute_tool(_fake_call("boom", "{}"))
        assert "工具执行失败" in out
        assert "ZeroDivisionError" in out   # 类型名要带上，模型才能判断怎么改

    def test_result_is_clipped(self, monkeypatch):
        """超长结果被截断 —— 否则每轮重发整段历史，上下文滚雪球撑爆"""
        monkeypatch.setitem(d32.TOOL_IMPL, "flood", lambda **kw: "y" * 9000)
        out = d32.execute_tool(_fake_call("flood", "{}"))
        assert len(out) <= d32.MAX_RESULT_CHARS + 100
        assert "结果被截断" in out


# ══════════════════════════════════════════════════
# L2 在线层
# ══════════════════════════════════════════════════
requires_key = pytest.mark.skipif(
    not os.environ.get("DEEPSEEK_API_KEY"),
    reason="需要 DEEPSEEK_API_KEY（无 key 时跳过，不算失败）",
)


class _ToolSpy:
    """包住 execute_tool，记录模型要过哪些工具，**同时照常返回真结果**。

    只计数不返回的 spy 会让循环拿到空结果、行为完全失真 —— spy 必须透明。
    """

    def __init__(self, real):
        self.real = real
        self.names = []

    def __call__(self, tool_call):
        self.names.append(tool_call.function.name)
        return self.real(tool_call)


@pytest.fixture(scope="module")
def client():
    """客户端建一次就够，别每个测试重建"""
    return d32.make_client()


@pytest.fixture
def spy(monkeypatch):
    """每个测试一个干净的计数器，包住 d32.execute_tool。

    run_tool_loop 里是模块级查 execute_tool，所以 patch 模块属性就能拦到。
    """
    s = _ToolSpy(d32.execute_tool)
    monkeypatch.setattr(d32, "execute_tool", s)
    return s


@requires_key
class TestLoopOnline:
    """模型行为。这些断言天生是概率的 —— 断言「结果语义」，
    不断言「调用序列」。原因见 test_diagnosis_reports_both_faults 的 docstring。
    """

    def test_greeting_calls_no_tool(self, client, spy):
        """「你好」→ 0 次工具调用。

        负向断言最稳，这是整个 L2 里最可靠的一条。它同时是坑表 #12 的机器判据：
        实测 description 写宽了（"不知道有哪些文件可用时先调用它"）问候也会调工具；
        收窄成「仅在用户要求排查系统问题时调用」立刻不调。
        **只改这一个变量行为就反转 —— 所以工具描述是 prompt，不是注释。**
        """
        d32.run_tool_loop(client, "你好")
        assert spy.names == [], f"问候不该调工具，却调了 {spy.names}"

    def test_diagnosis_reports_both_faults(self, client, spy):
        """「系统有什么异常吗」→ 报告要覆盖 fixture 里那两起并发故障。

        断言的是**结果语义**，不是调用序列。模型可以 list→read→search，
        也可以先 search 后 read，路径随便换；断言「第几步必须调哪个工具」
        是在测实现细节，模型换个走法就红，最后你只能把测试删掉。

        fixture 里其实有一整条故障时间线（DB 连不上 + 根分区写满 → 日志写不进去
        → 降级 → 服务 503 → 呼叫值班），但这里只断言**两起根因**，
        因为措辞是模型生成的，认「客观特征」而不是认「它怎么说」。
        """
        report = d32.run_tool_loop(client, "系统有什么异常吗")

        db = ["5432", "connection refused", "10.20.31.7", "数据库"]
        disk = ["No space left", "写满", "空间", "1.9"]
        assert any(k in report for k in db), f"没覆盖数据库故障：\n{report}"
        assert any(k in report for k in disk), f"没覆盖磁盘故障：\n{report}"

    def test_tools_actually_called_and_names_valid(self, client, spy):
        """诊断问题必须真的用工具；且模型要的工具名必须都在 registry 里。

        后半条能抓到一类真故障：description 改名了但 TOOLS 里的 name 没跟着改，
        模型就会要一个不存在的工具。
        """
        d32.run_tool_loop(client, "系统有什么异常吗")
        assert len(spy.names) >= 1, "诊断问题一次工具都没调，工具层没起作用"
        invalid = [n for n in spy.names if n not in d32.TOOL_IMPL]
        assert not invalid, f"模型要了不存在的工具：{invalid}"

    def test_loop_terminates(self, client):
        """max_rounds=1 时必须返回而不是打转 —— 循环要有刹车。

        注意这里故意**不断言**返回的是最终答案还是「超过 N 轮」提示：
        两种都是合法结果。这条测的是「会停」。
        """
        out = d32.run_tool_loop(client, "系统有什么异常吗", max_rounds=1)
        assert isinstance(out, str) and out.strip()


# ══════════════════════════════════════════════════
# 未处理项（都是设计选择或增强，不是 bug，因此写不出稳定断言）
# ══════════════════════════════════════════════════
# · 只限轮数不限调用数：MAX_ROUNDS=5，但一轮可返回 N 个 tool_call，
#   理论上是 5×N 次，没有总预算（总次数 / 总 token / 墙钟超时）
# · 工具串行执行：实测第 2 轮模型一次要了 read_log + search_log 两个，
#   协议按 tool_call_id 匹配、不按顺序，所以 IO 密集的工具本可以并行
# · read_log / search_log 全量读：截断只保护了上下文，没保护进程内存；
#   tail 语义该用 deque(f, maxlen=n)，真机日志动辄几个 GB
# · list_files 只 isfile + 不递归 → 目录和嵌套日志不可见
# · execute_tool 把异常原文喂回模型，FileNotFoundError 会带完整绝对路径
# · ALLOWED_ROOT 目前是写死的相对路径，真机应来自配置（如 /var/log/app）
