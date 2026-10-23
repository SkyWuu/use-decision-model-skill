# 代码信号：去哪里找候选

搜索结果只是线索。每条线索都要打开上下文确认：判断的是什么、输入是不是非结构化内容、结果被谁消费。下面的模式按语言常见写法给出，覆盖 Python、TypeScript / JavaScript、Go、Java、C / C++；其他语言照着改。

## 搜索前

- 在仓库根目录运行，每条命令末尾都显式写上搜索路径（`.` 或子目录）；不写路径时，`rg` 在没有终端的环境里可能去读标准输入，什么也搜不到。
- `rg` 默认跳过 `.gitignore` 里的目录；再排除依赖、构建产物、生成代码和第三方代码，例如加 `-g '!**/{vendor,third_party,node_modules,dist,build,gen,generated}/**'`。
- 第一轮先排除测试目录（`-g '!**/{test,tests,__tests__,testdata}/**'`）。测试在第 4 步找证据时再看。
- 能力档案里的 `llm_sdk_markers`、`decision_model_markers` 有值时，把它们加进对应的搜索。
- 没有 `rg` 就用 `git grep -n -E` 或 `grep -rnE`，模式基本通用（`\p{Han}` 需要 `rg` 或 `grep -P`）。
- 大仓库按子系统加路径参数逐个搜，不要一次输出几万行。

## 一、替换：已经有模型或规则在做的判断

### 1. LLM 调用

```bash
# SDK、网关和 HTTP 端点（同时搜调用处和封装函数）
rg -n -i -e 'chat/completions|/v1/(chat|messages|responses|completions|generate)' \
  -e 'chat\.completions\.create|messages\.create|responses\.create|ChatCompletion' \
  -e '\b(llm|LLM|Llm)[A-Za-z_]*\s*[.(]|llm_?gateway|LlmClient|ChatClient|complete\(|chat\(' .
# 常见 LLM 框架和客户端 import
rg -n -e '^\s*(from|import)\s+[\w.]*(openai|anthropic|litellm|langchain|llama_index|dashscope|zhipuai|qianfan)' \
  -e "require\(['\"][^'\"]*(openai|anthropic|langchain|ai)['\"]\)|from ['\"](@?[\w/-]*)(openai|anthropic|langchain|ai-sdk)['\"]" .
# C / C++、Go、Java 里手工拼 HTTP 请求
rg -n -e 'curl_easy_setopt|httplib::Client|brpc::Channel|cpr::Post' \
  -e 'http\.(Post|NewRequest)\(|RestTemplate|WebClient|OkHttpClient|HttpClient\.new' .
```

找到调用后，看三件事：prompt 要的是不是标签、是否或分数；输出是不是被解析成枚举；解析失败时怎么办。只要输出是有限答案，就是候选；输出是给人看的文本，就不是。

### 2. 要求输出标签的 prompt

```bash
# 中文 prompt
rg -n -e '只(能)?(回答|输出|返回)|仅(回答|输出|返回)|请从(以下|下列)|以下(类别|选项|分类)|(选择|选出)其中?一个|属于哪(一)?(类|种|个)' \
  -e '判断(一下)?(是否|用户|该|这)|回答[“"]?(是|否)|是或否|输出(类别|标签|分类|意图|结果)|返回\s*JSON|(打|评)分|[1１]\s*[-~到至]\s*[0-9０-９]+\s*分' .
# 英文 prompt
rg -n -i -e 'respond (only )?with|answer (only )?with|reply with (only )?|one of the following|choose (one|from)' \
  -e 'return (only )?(a )?json|classify|categori[sz]e|which (category|label|intent)|yes or no|on a scale of' .
```

C / C++ 的 prompt 常在 `R"(...)"` 原始字符串或多段 `std::string` 拼接里，Java 常在 `"""` 文本块或资源文件里，Python 常在模板文件（`prompts/`、`*.txt`、`*.j2`、`*.yaml`）里，要一起搜。

### 3. 把模型输出解析成标签

```bash
rg -n -e '\.strip\(\)|\.trim\(\)|TrimSpace\(' -e '\.(lower|upper)\(\)|toLowerCase\(\)|toUpperCase\(\)' \
  -e 'json\.loads\(|JSON\.parse\(|json\.Unmarshal\(|readValue\(' \
  -e '\[["'"'"'](label|category|intent|class|score|decision|answer)["'"'"']\]' \
  -e 'startswith\(|startsWith\(|HasPrefix\(' -e '==\s*["'"'"'](是|否|yes|no|YES|NO|true|false)["'"'"']' .
```

这一组噪声大，只在 LLM 调用附近看。典型形态：`resp.strip()` 后 `in CATEGORIES`、`startsWith("YES")`、`find("退款") != std::string::npos`、解析失败就重试或归为默认值。

### 4. 冒充语义判断的规则

```bash
# 关键词表、黑白名单
rg -n -e '(KEYWORDS?|keywords?|WORDS|words|TERMS|PHRASES|PATTERNS|BLACKLIST|blacklist|WHITELIST|whitelist|BLOCKLIST|STOPWORDS|SYNONYMS)\w*\s*(:?=|=)' \
  -e 'k[A-Z]\w*(Words|Keywords|Terms|Patterns)\b' .
# 中文字符串数组（关键词表的常见形态）
rg -n -e '[\[{(]\s*["'"'"'][\p{Han}]{1,12}["'"'"']\s*,\s*["'"'"'][\p{Han}]{1,12}["'"'"']' .
# 正则和字符串包含
rg -n -e 're\.(search|match|findall|compile|sub)\(|regexp\.(MustCompile|MatchString|Compile)\(|Pattern\.compile\(|std::regex|new RegExp\(|/[^/\n]{3,}/[gimsuy]*\.test\(' \
  -e '\bin\s+(text|content|message|msg|body|title|query|comment|desc\w*)\b' \
  -e '\.includes\(|\.contains\(|strings\.Contains(Any)?\(|strstr\(|\.find\([^)]*\)\s*!=\s*(std::)?string::npos|indexOf\(' .
# 相似度、模糊匹配阈值
rg -n -i -e '(similar\w*|cosine|sim|score|distance|dist|threshold|ratio)\w*\s*[<>]=?\s*[0-9]*\.[0-9]+' \
  -e 'levenshtein|fuzz|jaccard|simhash|minhash|difflib' .
```

判断标准：规则匹配的是不是在近似一个语义概念（紧急、投诉、违规、重复、相关、意图）。匹配格式、ID、协议字段的正则不是候选。

## 二、增强：今天隐含地做了决定

| 代码信号 | 隐含的决定 | 题型方向 | 护栏 |
| --- | --- | --- | --- |
| `results[0]`、`.first()`、`.get(0)`、`front()`、模糊匹配加 `LIMIT 1` | 第一个就是对的 | 有两个以上候选时，选择题选哪个，带“都不符合” | 候选召回是上限；只有一个候选时保持确定性 |
| `TOP_K = 5`、`[:20]`、`.slice(0, 10)`、相似度阈值截断 | 第 k 名之后都不重要 | 对更大的候选池逐个做是非题或等级题 | 上下文预算和最终排序仍由代码定 |
| 所有用户同一个排序、文案或选项 | 一刀切 | 按用户状态做选择题或等级题 | 用户显式设置永远优先 |
| 工具调用、外部动作执行后不看结果 | 执行了就算成功 | 是非题：结果是否符合意图 | 失败时重新观察或转人工，不发明新动作 |
| 只在发布、提交、运行结束时检查 | 问题可以等 | 每一步对具体缺陷做是非题 | 缺陷定义要窄，统计误报 |
| 随机抽样或只看第一页审核 | 其余都没问题 | 全量做是非题，人只看不确定的部分 | 抽样审核保留，作为基准 |
| 所有条目都进人工队列 | 每条都得人看 | 是非题或等级题给队列排序、预标注 | 钱、权限、政策规定必须人定的事仍由人定 |
| 吞掉异常、`?? "other"`、默认归为“其他” | 拿不准就假装知道 | 明确的“无法判断”出口加复核路径 | 记录无法判断的样本，不把它们硬套成动作 |

```bash
# 取第一个
rg -n -e '\b\w*(results?|hits|matches|candidates|items|docs|rows|options)\w*\[0\]' \
  -e '\.first\(\)|\.get\(0\)|\bfront\(\)|FirstOrDefault\(|\bLIMIT 1\b|findFirst\(\)' .
# 固定 top-k 和截断
rg -n -i -e '\btop_?k\b|TOP_?K|TOP_?N|MAX_(RESULTS|CANDIDATES|ITEMS)' \
  -e '\[:\s*\d+\s*\]|\[\s*:\s*[A-Z_]+\s*\]|\.slice\(0,\s*\w+\)|\.Take\(\d+\)|\blimit\s*[:=]\s*\d+|\bLIMIT \d+' .
# 抽样审核
rg -n -i -e 'random\.random\(\)\s*<|rand\.(Float64|Intn)\([^)]*\)\s*<|Math\.random\(\)\s*<|ThreadLocalRandom' \
  -e 'sample_?rate|SAMPLE_?RATE|sampling|抽样|抽检' .
# 人工审核队列
rg -n -i -e 'manual_?review|needs_?review|human_?review|review_?queue|pending_?(review|approval)|escalat' \
  -e '人工(审核|复核|处理|审批)|待(审核|审批)|转人工' .
# 悄悄兜底
rg -n -e 'except(\s+\w+)?(\s+as\s+\w+)?\s*:\s*(pass|return\b|continue)' -e '\?\?\s*["'"'"']' \
  -e '\bor\s+["'"'"'](other|unknown|default|其他|未知)["'"'"']' -e 'catch\s*\([^)]*\)\s*\{\s*\}' .
# 执行外部动作的地方（再看执行后有没有检查结果）
rg -n -i -e 'subprocess\.|exec\.Command\(|child_process|execSync|spawn\(|system\(|popen\(' \
  -e 'run_?tool|call_?tool|execute_?tool|tool_?call' .
```

## 三、新能力：以前没做的判断

先列出系统里的高频对象：消息、工单、评论、帖子、日志、告警、文档、检索结果、agent 步骤、工具调用、用户。对每个对象、每个流程环节，问下面这些问题：

1. **每一条，而不是抽样？** 哪里只看了样本、第一页或根本没人看？
2. **每一步，而不是最后？** 哪里的检查只在发布、提交、运行结束时做？
3. **每个用户，而不是统一默认？** 哪里所有人得到同一个排序、文案、节奏？
4. **在昂贵操作之前？** 大模型调用、人工、付费接口、通知、慢任务——能不能先判断要不要做、做哪一种？
5. **在另一个 AI 之后？** 哪里直接信任了另一个模型的抽取、引用、工具调用或回复？
6. **实时？** 打字、搜索、对话、agent 操作页面这类循环里，判断够快会不会改变体验？
7. **在历史数据上？** 积累了却没人读的工单、评论、日志、轨迹，能不能回填标签、清理数据、找规律？
8. **给已有模型加特征？** 排序、预测、风控模型是不是只用了结构化字段？
9. **持续清理？** 重复记录、过期内容、文本和结构化字段互相矛盾的情况，能不能在写入时就判断？
10. **给人工预标注？** 逐条看的队列，能不能排好序并附上判断依据？
11. **判断一次、到处复用？** 内容质量、话题、是否标题党这类不随用户变化的属性，能不能在内容写入时判断一次、存成字段，让排序、推荐、搜索按需组合？这样调用量跟内容数走，而不是跟“用户 × 内容”走。
12. **执行之后？** 工具调用、外部请求、自动化操作执行完，有没有人看结果是不是达到了目的？

每个想法都要锚定才算候选：

- 服务哪个流程环节，想改善哪个指标；
- 判断的对象是什么，数据已经在哪里（文件、表、字段、事件）；
- 结果在哪个文件或函数里被消费，消费后行为怎么变；
- 为什么今天没做（太贵、太慢、规则太脆，还是没人想到）；
- 题型草稿和错判代价。

至少写出三个候选再开始锚定。锚不住的想法不进清单；只差一项关键信息的，可以放进“信息不足”一节，写明待确认什么。

## Agent 和 LLM 编排项目：四桶分类

把 agent 循环里的每一步分进四个桶：

| 这一步要…… | 交给 | 例子 |
| --- | --- | --- |
| 生成文本、代码或计划 | 生成式模型 | 写回答、写补丁、写摘要、写给人看的理由 |
| 遵守精确规则 | 代码 | 重试上限、预算、白名单、日期和计数 |
| 从能事先列出的答案里选 | 决策模型 | 选工具、选模型档位、保留或丢弃、是否完成、是否安全、是否偏题 |
| 做不可撤销的事 | 人或代码审批 | 发送、付款、发布、删除、改权限、强推代码 |

第三个桶通常比想象的大，是主要候选来源。常见位置：

- 每步调用 LLM 问“任务完成了吗”“下一步用哪个工具”；
- 用 LLM 选模型、选技能、选子 agent；
- 上下文压缩前决定哪些历史还有用；
- 工具调用执行前的风险判断、执行后的结果检查；
- 检索结果、记忆条目的取舍。

护栏：用户显式指定的模型、工具永远优先；危险命令的确定性拦截保留，模型判断只作补充；选择题的选项由代码从合法动作里生成。

## 不要列的东西

- 运行良好的确定性解析：CSV、JSON、ISO 日期、ID、协议字段。
- 输出是给人看的文本的步骤（但可以在它之后加检查）。
- 数值计算、聚合、联表、排序规则本身。
- 没有文本化步骤的图片、音频判断（能力档案支持图片时除外）。
- 其实是数据库查询的“判断”。
- 一天只发生几次、错了也无所谓的判断。
