# 模型映射与 API Key 配置方案（阶段 06 之前必须落地）

> 对应 WorkBuddy 待办：② 模型信息（`MODEL_PROVIDER_BASE_URL` + `API_KEY` + L1/L2/L3 映射）。
> 原则不变：**型号只出现在配置层，业务代码只认 L1/L2/L3**（红线 3）。
> 价格与型号会变，本文以官方现网为准、并标注核对日期。

---

## 0. 模型核实结论（核对日期 2026-09-23）

**`deepseek-v4.1-flash` 真实存在**，为 DeepSeek 2026-09-10 正式发布的 V4.1 Flash：

- 552B 参数 MoE，全新 Causal-Encoder-Decoder 结构，输入激活 8B / 输出激活 16B，**原生多模态**，1M 上下文、最大输出 384K。
- 官方称其在性能 / 费用 / 速度 / 总用时上**全面超越 V4 Pro**，因此在被 V4.1 Pro 取代前，`deepseek-v4-pro` 的请求被路由至 V4.1 Flash。
- **API 模型名已改为 `deepseek-flash`**。旧名 `deepseek-v4-flash`、`deepseek-v4-flash-vision-exp` 已下线但暂被路由到 V4.1 Flash（兼容用途）。
- 编程/Agent 场景专项优化：V4.1 Flash 在**训练阶段就针对 DeepSeek Harness 的标准模式、PTC（程序化工具调用）模式、极简模式**做了适配（见 Harness 版本 V0.1.5）。
- WorkBuddy / CodeBuddy / OpenCode 为官方合作接入方。

> ⚠️ 命名提醒：**配置里请写 `deepseek-flash`**，不要再写 `deepseek-v4.1-flash`（这是发布代号，非 API 名）或 `deepseek-v4-flash`（已下线，仅兼容路由）。

---

## 1. 结论：等级 → 型号映射

原计划设想 L1/L2/L3 三个不同型号；核实后发现**当前在售主力已合并为一个模型**（`deepseek-flash` 取代了 V4 Flash 与 V4 Pro 两条线）。因此 V1 更合理的映射是**同型号 + 思考模式/推理档位区分**：

| 等级 | 用途（计划 11 节） | 型号 | 推理档位 | 上下文 |
|---|---|---|---|---|
| **L1** | 归类、摘要、简单解释 | `deepseek-flash` | 非思考 | 1M |
| **L2** | 相关性、多错误归纳 | `deepseek-flash` | 思考（low） | 1M |
| **L3** | 复杂根因、跨时间簇困难推理 | `deepseek-flash` | 思考（high/max） | 1M |

- 供应商：**DeepSeek 官方 API**，OpenAI 兼容端点。
- `MODEL_PROVIDER_BASE_URL = https://api.deepseek.com`（Anthropic 兼容可另用 `/anthropic`）。
- 若你仍想 L3 用不同型号，可把 L3 设为 `deepseek-v4-pro`（DeepSeek-V4-Pro-0813 仍在售）；但 V4.1 Flash 在 Agentic Benchmark 上已超 V4 Pro，**不建议**为「更聪明」而把 L3 退回 V4 Pro——反而更慢更贵。

> 说明：以上映射**只改 `.env`**，业务代码零改动，满足阶段 06 验收「切换型号只改环境变量」。

---

## 2. 定价（官方 api-docs.deepseek.com，核对日期 2026-09-23）

货币单位：**人民币元 / 每百万 tokens**。**峰谷定价**：闲时 = 高峰的一半。

高峰时段 = 工作日（周一至周五）北京时间 **9:00–12:00 与 14:00–18:00**；其余时间（含周末全天）为闲时。

| 模型 | 输入（缓存命中）闲/峰 | 输入（缓存未命中）闲/峰 | 输出 闲/峰 |
|---|---|---|---|
| **`deepseek-flash`（V4.1 Flash）** | 0.02 / 0.04 元 | **1 / 2 元** | **4 / 8 元** |
| `deepseek-v4-pro`（V4-Pro-0813） | — | 9 元 | 27 元 |

- USD 参考（≈7.1，第三方核对 2026-09-18）：Flash 闲时 $0.15 输入(未命中) / $0.003 缓存命中 / $0.60 输出；高峰翻倍。
- 缓存命中 vs 未命中价差达 **50 倍**——Agent 反复读系统提示/代码仓/历史上下文时命中率高，**成本因此进一步下降**。这也意味着：计划阶段 07 的成本控制要**把缓存命中/未命中分开计费**，否则会高估成本。

---

## 3. `.env` 应填入的内容

```dotenv
# 供应商（OpenAI 兼容）
MODEL_PROVIDER_BASE_URL=https://api.deepseek.com
MODEL_PROVIDER_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# 等级 → 型号（只在这里出现型号）
MODEL_L1=deepseek-flash
MODEL_L2=deepseek-flash
MODEL_L3=deepseek-flash

# 推理档位（Gateway 内部消化；L1 关，L2/L3 开）
MODEL_L1_REASONING=off
MODEL_L2_REASONING=low
MODEL_L3_REASONING=high

# 成本单价：每百万 tokens；货币单位 1（如 1=CNY）。以下取「闲时价」，高峰时按 2 倍算
MODEL_L1_PRICE_INPUT_PER_1M=1
MODEL_L1_PRICE_OUTPUT_PER_1M=4
MODEL_L2_PRICE_INPUT_PER_1M=1
MODEL_L2_PRICE_OUTPUT_PER_1M=4
MODEL_L3_PRICE_INPUT_PER_1M=1
MODEL_L3_PRICE_OUTPUT_PER_1M=4

# 缓存命中价（阶段 07 成本控制区分命中/未命中时用）
MODEL_CACHE_HIT_INPUT_PRICE_PER_1M=0.02
```

> 若阶段 07 只做「粗略成本」，可先用上面的闲时价；要精确就得区分峰谷与缓存命中——建议在 Cost Controller 里预留这两个维度。

---

## 4. 怎么拿到 API Key（你需要做的三步）

1. 打开 `https://platform.deepseek.com/` 注册并登录；
2. 进入 **充值**（按量付费，建议先充小额，如 ¥20，够跑几十次分析）；
3. 进入 **API Keys → 创建 API Key**，复制 `sk-...` 串（只显示一次），填入 `.env` 的 `MODEL_PROVIDER_API_KEY`。

验证连通（PowerShell）：

```powershell
curl https://api.deepseek.com/chat/completions `
  -H "Content-Type: application/json" `
  -H "Authorization: Bearer $env:MODEL_PROVIDER_API_KEY" `
  -d '{"model":"deepseek-flash","messages":[{"role":"user","content":"ping"}],"max_tokens":5}'
```

> 说明：我无法替你注册与充值（涉及账号与支付），也**不应**持有你的 Key。Key 只进 `.env`、被 `.gitignore` 挡住、不进代码、不进镜像。

---

## 5. 单次分析成本预估（用于 `run_max_cost` / 月度预算）

按计划 09 节的 token 预算：L1≈2000、L2≈4000、L3≈8000 输入 token；输出按 ~25% 估。用 `deepseek-flash` 闲时价（输入 1 元 / 输出 4 元 每百万）：

| 等级 | 输入(估) | 输出(估) | 单次成本（闲时, CNY） | 高峰(×2) |
|---|---|---|---|---|
| L1 | 2000 | 500 | 2000×1e-6 + 500×4e-6 ≈ **¥0.004** | ¥0.008 |
| L2 | 4000 | 1000 | 4000×1e-6 + 1000×4e-6 ≈ **¥0.008** | ¥0.016 |
| L3 | 8000 | 2000 | 8000×1e-6 + 2000×4e-6 ≈ **¥0.016** | ¥0.032 |

- **即使全部走 L3，单次也在 ¥0.02–0.03 量级**（远低于上一版方案里按 V4-Pro 12/24 元估的 ¥0.14）。
- 因此预算上限**完全不会**摧毁分析价值（对应计划 21 节假设）。示例里的 `MONTHLY_BUDGET` / `DEFAULT_RUN_MAX_COST` 若沿用，请注意**货币单位统一为元**。
- 真正要盯的是**幻觉与证据质量**，不是钱。

---

## 6. 与文档的一致性

- 计划阶段 01 的骨架里已补 `MODEL_{L1,L2,L3}_PRICE_{INPUT,OUTPUT}_PER_1M`——本方案沿用同一命名。
- 计划阶段 06 验收「切换型号只改环境变量、不改业务代码」——本方案满足。
- 结构化输出：`deepseek-flash` 官方标注支持 **Json Output / Tool Calls / Responses API / Anthropic API**，足以承载计划 09 节四层语义的 JSON Schema 约束与工具调用。
- 与 DeepSeek Harness 的关系见《交接DeepSeek Harness就绪度评估.md》。
