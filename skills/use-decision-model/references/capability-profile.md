# 决策模型能力档案

这份档案描述公司自研决策模型的能力边界。Skill 用它判断哪些建议可行、放在哪一节。

- 由平台团队或使用方填写。改的是本文件，不是目标项目。
- 没填的字段保持 `null` 或空列表。Skill 会按下文“未填写时的保守默认”处理，并在报告末尾的说明里写明。
- 用户在对话里直接给出的参数优先于本档案。
- 档案只用于判断，报告里不写模型名称。

## 档案

```yaml
# ---- 身份与接入（不进报告正文）----
model_name: null              # 内部名称。只用来识别项目里已有的接入，不写进报告
access_entry: null            # 接入文档或平台入口。只在报告末尾的说明里出现

# ---- 题型 ----
question_types:
  yes_no: true                # 是非题：返回条件成立的概率（协议里常叫 noul / predicate）
  choice: true                # 选择题：从互斥选项里选一个（choice）
  score: true                 # 等级题：在有序等级上的位置（score）
  box: null                   # 图片框选（box）。null 视为不支持
choice_max_options: null      # 选择题选项数上限
score_max_levels: null        # 等级题等级数上限

# ---- 输入 ----
image_input: null             # 是否接受图片输入：true / false
context_limit_tokens: null    # 状态 + 题目的上下文上限（token）
chinese_quality: null         # 中文效果："已验证" / "未验证"；可附评测链接或结论

# ---- 运行与部署 ----
latency: null                 # 延迟量级，例如 "百毫秒级"；可附测量来源
deployment: null              # 部署方式，例如 "内网部署" / "公有云"
data_leaves_domain: null      # 数据是否出域：true / false
cost_basis: null              # 成本口径，例如 "按输入 token 计费，输出不计费"

# ---- 已知情况 ----
known_weaknesses: []          # 平台评测发现的弱点，例如 "长于 4K token 的中文对话准确率明显下降"
llm_sdk_markers: []           # 项目里常见的内部 LLM SDK、网关地址或封装函数名，用于扫描
decision_model_markers: []    # 项目里已有的决策模型接入标识（SDK 名、接口路径），用于识别已在用的点
checked_at: null              # 本档案最近一次核对的日期，例如 "2026-10-21"
```

## 各字段怎么用

| 字段 | Skill 用它做什么 |
| --- | --- |
| `model_name` | 识别项目里已经接入的地方，避免重复建议；报告里不写 |
| `access_entry` | 写进报告末尾的说明，告诉读者去哪里接入 |
| `question_types` | 不支持的题型不用来写题；需要它的候选改写成其他题型或淘汰 |
| `choice_max_options` | 选项超过上限的选择题改成分层选择（先选大类再选小类）或逐个候选的是非题 |
| `score_max_levels` | 等级题的等级数不超过上限 |
| `image_input` / `box` | 决定涉及截图、图片的判断能不能进清单 |
| `context_limit_tokens` | 判断状态放不放得下；放不下的要在代码里先裁剪、检索，或拆成逐条判断 |
| `chinese_quality` | 决定中文输入为主时，要不要在末尾说明里提醒先用真实中文样本验证 |
| `latency` | 写潜在收益时能不能提延迟、提到什么程度 |
| `deployment` / `data_leaves_domain` | 和数据敏感度一起判断个人或受监管数据能不能交给模型 |
| `cost_basis` | 写潜在收益时能不能谈成本，以及按什么口径谈 |
| `known_weaknesses` | 在通用局限之外补充排除项 |
| `llm_sdk_markers` | 加进第 3 步的搜索模式，找到经内部网关封装的 LLM 调用 |
| `decision_model_markers` | 找到已在用决策模型的地方，写进报告末尾的说明，不再重复建议 |
| `checked_at` | 档案超过半年没核对时，在报告末尾的说明里提醒 |

## 未填写时的保守默认

| 字段 | 未填写时怎么处理 |
| --- | --- |
| 题型 | 只用三种文本题型：是非题、选择题、等级题；不用图片框选 |
| `choice_max_options` | 选项控制在 20 个以内；更多时改成分层选择 |
| `score_max_levels` | 等级题用 3–5 级，不超过 10 级 |
| `image_input` | 视为不支持。涉及图片的判断必须先有文本化步骤（OCR、已有的文字描述），否则不进清单 |
| `context_limit_tokens` | 不假设长上下文。状态短小的点（一条消息、一张工单、一对记录、一个候选）正常评估；状态可能很长的点（整段对话、长文档、一次塞几十个候选），先在代码里裁剪或改成逐条判断 |
| `chinese_quality` | 视为未验证。以中文输入为主时，在末尾说明里写明中文效果未验证、上线前先用 50–200 条真实中文样本评估 |
| `latency` | 不写具体延迟数字。替换生成式调用时可以定性地说“单次判断、不生成文字，更快”，不承诺提速倍数 |
| `deployment` / `data_leaves_domain` | 视为未知。有建议涉及个人或受监管数据时，在末尾说明里写“需确认部署方式和数据是否出域” |
| `cost_basis` | 不估算金额和节省比例；替换生成式调用时可以定性地说更省 |
| `known_weaknesses` | 只按 `primitives-and-limits.md` 的通用局限处理 |
| `llm_sdk_markers` | 只用 `signals.md` 里的通用模式搜索；可能漏掉内部网关封装的调用，在末尾说明里写一句 |
| `decision_model_markers` | 不做“已在用”识别 |
| `checked_at` | 档案其他字段已填时，在末尾说明里写“档案核对日期未知” |

整份档案都没填时，末尾说明里写一句“能力档案未填写”，再点出对本次建议影响最大的两三项（通常是中文效果、上下文上限、数据是否出域），不逐个字段列。

## 填写示例

下面只是格式示例，数值不代表任何真实模型：

```yaml
model_name: "dm-example"
access_entry: "内部决策模型平台 → 接入指南"
question_types: {yes_no: true, choice: true, score: true, box: false}
choice_max_options: 50
score_max_levels: 10
image_input: false
context_limit_tokens: 8000
chinese_quality: "已验证：内部中文工单评测集，与人工标注一致率达标"
latency: "百毫秒级（平台压测报告）"
deployment: "内网部署"
data_leaves_domain: false
cost_basis: "按输入 token 计费，输出不计费"
known_weaknesses: ["数字比较不可靠", "长对话末尾的信息容易被忽略"]
llm_sdk_markers: ["llm_gateway", "/v1/chat", "LlmClient"]
decision_model_markers: ["dm_client", "/v1/decide"]
checked_at: "2026-10-21"
```
