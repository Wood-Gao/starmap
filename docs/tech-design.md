# starmap 技术设计 v0.1

状态：独立评审及复审完成，待作者最终 review。本文是实现方案，不代表功能已实现。

## 1. 已确认的产品决策

| 决策 | 方案 |
| --- | --- |
| 入口 | 用户描述需求，由 AI 编写任务卡并调用框架 |
| 项目边界 | 全局 Rules 独立使用；开发框架以队列和执行层抽象为核心 |
| 技术方案 | Python、JSON 任务卡、SQLite 运行状态，轻量且便于迁移 |
| 首个后端 | 本地 Codex 独立会话，独立评审也使用全新 Codex 会话 |
| 并发 | 实现、评审分别限流，同时遵守共享资源总上限 |
| 插队 | 非抢占，只改变待派发任务的顺序 |
| 依赖满足 | 前置任务独立评审通过且产物可用，不要求合并主分支 |
| 移出 | 后续依赖任务保持阻塞 |
| 代码隔离 | 每任务独立 Git worktree；框架负责本地提交与前置产物集成 |
| 冲突 | 阻塞并交给用户处理，不自动推送或合并主分支 |
| 返工 | 默认最多自动修复两轮，之后等待人工处理 |
| 修复预算 | 必要测试失败与评审退回共享两轮修复预算 |
| 基线冻结 | 首次派发时固定本地基线；显式 SHA 始终固定，不自动 fetch |
| 修改任务约定 | 移出旧任务并新建任务，受影响下游显式重建依赖链 |

下面的模块、数据结构和算法是拟议实现选择，最终 review 前可以调整。Python 最低版本拟定为 3.9，以覆盖当前机器环境；业务核心优先使用标准库。

## 2. 首版范围

首版支持单机、单个调度进程、多个本地 Codex 子进程以及多个任务；每个任务及其依赖限于同一注册仓库。提供 JSON 输入输出的 CLI 供 AI 调用。一次用户需求可以由 AI 拆成多个相互依赖的任务，框架不自行调用模型拆解需求。

首版包含持久化队列、能力匹配、并发、依赖、插队、移出、实现、独立评审、有限返工、恢复及配置式执行窗口。真实分时价格、远端后端、跨仓库依赖、分布式调度、Web 和 MCP 接口后续扩展。

24 小时持续开发是运行目标；单机休眠、断网、额度耗尽和权限不足会影响实际可用性。首版记录阻塞及故障，不能承诺持续产出或固定成本节省。

## 3. 进程与模块

```text
用户 → 对话 AI → JSON 任务卡 → starmap CLI → SQLite
                                              ↑
                                    scheduler run（唯一实例）
                                      ↓              ↓
                                workspace       backend registry
                                      ↓              ↓
                                task runner → Codex adapter
                                      ↓              ↓
                                artifacts       实现/评审独立进程
```

| 模块 | 责任 |
| --- | --- |
| contracts | 任务卡、后端配置、执行输入输出校验与版本化 |
| store | 事务、约束、事件、迁移及容量预留 |
| scheduler | 依赖检查、排序、窗口、能力匹配、配额与派发 |
| runner | 管理一次尝试，持久化启动与退出记录，捕获日志，响应停止请求 |
| backends | 后端协议、能力声明、mock 与 Codex 适配器 |
| workspace | worktree、依赖提交集成、快照和产物验证 |
| verification | 运行约定检查，保存命令、退出码、输出及版本 |
| review | 构造独立评审输入，校验结论，决定返工或交付 |
| cli | 校验、入队、控制、查询及机器可读错误 |

CLI 直接以短事务操作数据库；只有调度器可以派发。调度器持有状态目录上的 OS 级独占锁，第二实例立即失败。数据库事务中不执行网络、Codex 或 Git 操作。

## 4. 数据与目录

```text
<state-root>/
  config.json
  state.sqlite3
  scheduler.lock
  cards/<task-id>/<revision>.json
  runs/<attempt-id>/
    input.json
    events.jsonl
    stderr.log
    result.json
    exit.json
  artifacts/<artifact-id>/manifest.json
  worktrees/<repo-id>/<task-id>/
```

源码仓库与运行状态分离，状态默认不进 Git。上述路径只是建议布局，不写死绝对路径。注册仓库记录本机路径，迁移时需要重新映射。worktree 依赖原仓库，不能只复制目录就认为完成迁移。

SQLite 是已接收任务与运行状态的唯一事实来源。JSON 卡在入队时经校验并作为不可变版本保存到数据库，文件是可重建的可读副本；文件写入失败不造成半条已接收任务。磁盘上的卡片修改不会被自动扫描执行。

建议表：

| 表 | 关键字段及约束 |
| --- | --- |
| repositories | id、path、default_base_ref、配置版本 |
| task_revisions | task_id、revision、规范化卡片、内容摘要；联合唯一 |
| tasks | id、current_revision、phase、disposition、priority、queue_order、repair_round、block_reason、generation |
| dependencies | task_id、predecessor_id、固定 revision；同仓库、无环 |
| attempts | id、task_id、revision、generation、role、backend_id、配置快照、状态、外部 ID、时间、result_digest |
| reservations | attempt_id、资源 key、占用量；尝试结束或确认停止后释放 |
| artifacts | id、task_revision、base_sha、head_sha、依赖产物清单、验证与评审引用 |
| events | 单调序号、任务/尝试、事件类型、时间、请求 ID、操作者、详情 |
| requests | request_id、操作、输入摘要、结果；防止调用方重试重复创建任务 |

SQLite 使用外键和显式写事务。初版选择默认回滚日志模式，简化离线迁移；写操作串行且保持短小。数据库与状态目录放本地磁盘，不承诺网络文件系统行为。暂不引入 Redis、消息中间件或 ORM。

## 5. 任务卡与 AI 调用接口

下例为拟议格式；模型名称由后端配置选择，示例不虚构可用模型 ID。

```json
{
  "schema_version": 1,
  "id": "add-input-validation",
  "repository": "demo",
  "base_ref": "main",
  "goal": "为指定接口增加输入校验，保持合法请求兼容",
  "scope": {"allowed_paths": ["src/", "tests/"], "forbidden_paths": ["migrations/"]},
  "depends_on": [],
  "acceptance": [{"id": "AC-1", "description": "合法和非法输入均符合接口约定", "checks": ["api-tests"]}],
  "implementation": {"backend": "local-codex", "required_capabilities": ["git", "python"]},
  "review": {"backend": "local-codex-review", "max_repair_rounds": 2},
  "schedule": {"priority": 0, "window": "anytime"},
  "delivery": {"mode": "local_commit"}
}
```

任务卡引用仓库配置中的检查 ID，不能自行注入任意 shell 命令；检查由明确 argv、cwd 和超时组成，不通过 shell 拼接执行。项目代码及检查本身仍属于可信项目执行内容，不能将 argv 形式当作安全隔离。

入队校验包括 ID、字段、仓库、ref 当前可解析性、路径、权限、验收覆盖、已注册检查和后端、依赖存在且无环。仓库必须有起始提交。入队检查 ref 不等于冻结分支提交，冻结时机见第 9 节。路径约束是验收与改动检查规则，不宣称是操作系统访问控制。

拟议 CLI（均尚未实现）：

```text
starmap task validate --file task.json --json
starmap task submit --file task.json --request-id <id> --json
starmap task submit-batch --file tasks.json --request-id <id> --json
starmap task list --json
starmap task show <id> --json
starmap task prioritize <id> --before <other-id> --request-id <id> --json
starmap task remove <id> --request-id <id> --json
starmap task retry <id> --request-id <id> --json
starmap task resolve-conflict <id> --expected-generation <n> --request-id <id> --json
starmap attempt reconcile <attempt-id> --expected-generation <n> --json
starmap attempt resolve <attempt-id> --confirmed-stopped --reason <text> --expected-generation <n> --json
starmap scheduler run
starmap backend list --json
starmap doctor --json
```

stdout 只输出 JSON 结果，诊断进 stderr。错误包含稳定 code、message、retryable、details；阻塞原因通过查询返回。相同 request_id 和相同输入返回原结果，不同输入报冲突。批量提交在一个事务中校验完整 DAG 并全部接收或全部拒绝。

首版不支持隐式修改已入队任务约定。需要修改目标、依赖或验收条件时，显式移出旧任务并提交新 ID；不得复活已经取消的任务或静默改写下游依赖。retry 只适用于未取消、约定未改变的失败或待人工任务，增加 generation，保留历史。

此限制及重建受影响下游链的操作方式已经作者确认。remove 先返回受影响下游清单；操作本身不级联取消下游。对话 AI 必须明确提交移出和重建操作，不能把重排优先级当作修改任务约定。

reconcile 只核对外部执行状态，不直接信任用户提供的“成功”结论。无法自动核对时，resolve 要求操作者提供确认旧执行已停止的说明，记录审计事件并再次做本机可验证检查；不能确认停止则保持 unknown。人工不能通过该命令伪造评审通过或绕过验证。

## 6. 状态与调度

将任务阶段、处置状态和尝试状态分别记录，避免把等待资源视为执行失败。

| 字段 | 值与含义 |
| --- | --- |
| phase | implement → verify → review → delivered；评审需修改时回 implement |
| disposition | active、needs_attention、failed、cancel_requested、cancelled |
| attempt.status | reserved、starting、running、unknown、succeeded、failed、stopped |
| block_reason | dependency、capacity、window、no_backend、integration_conflict、unknown_execution 等结构化原因 |

delivered 只能由当前 generation 的评审通过且全部必要检查通过、产物可用时进入；交付后产物不可变。处置进入 cancelled 或任务交付后不再自动派发。取消与完成竞态由数据库内的状态及 generation 校验决定，先成功提交的有效转换生效。

每轮调度：

1. 收集已结束 runner 的结果，核对尝试、revision 和 generation，再更新状态。
2. 重新计算依赖及产物是否可用、运行窗口和后端能力。
3. 按 priority 降序、queue_order 升序、task_id 稳定排序合格候选。
4. 在事务中重新检查状态及全局/角色/资源池容量，预留所有需要的额度并创建尝试。
5. 事务提交后启动 runner；确定未启动则失败并释放，启动情况未知则进入核对流程。

优先级操作作用于同一角色的等待顺序，不打断运行；持续高优先级流量可能导致饥饿，首版在查询中展示等待时长，自动老化策略后续再做。容量配置拟议默认全局 2、实现 2、评审 1，均可配置，不视为用户硬性要求。验证和 Git 集成也必须占用对应任务工作槽位；同一任务任意时刻只有一个写入者。

调低并发时保留已有占用，停止新派发直至满足上限。所有额度和尝试创建同一事务提交，释放具有幂等性。

实现尝试的容量覆盖工作区准备、Codex 编码、候选提交和 verify：Codex 子进程结束后尝试仍处于 running，只更新 substage；验证完成后才终结尝试并释放实现额度，再排队等待评审。验收类测试失败按第 9 节进入共享预算的修复流程；明确环境故障按第 10 节处理。评审占用独立的评审额度，不占用已结束的实现额度。一个任务不得同时有两个未终结尝试，unknown 也视为未终结。

## 7. 执行层协议

模型、机器和平台分别建模。后端描述包含 adapter、location、model（允许平台管理）、capabilities、roles、resource_pools 和 schedule。后端配置变更不改变已启动尝试的快照。

| 接口 | 输入/输出及语义 |
| --- | --- |
| describe | 能力、限制、角色、取消/恢复/请求去重及查询能力 |
| start | ExecutionSpec + request_id → handle 或明确错误；超时可返回 unknown |
| poll | handle → 状态、进展、外部执行 ID；断连不等于失败 |
| collect | handle → 标准结果、产物、证据或评审意见 |
| cancel | handle → 已请求、已停止或不支持；请求成功不代表停止 |

ExecutionSpec 包含任务约定版本、阶段、规则快照、依赖产物、工作区、实际模型配置、超时及可用权限。模型文本与日志是非可信结果，必须经格式和版本校验，不能直接充当调度命令。

标准结果的框架 envelope 包含 schema_version、task_id、revision、generation、attempt_id、role、candidate_sha、backend_id、exit_code 和证据引用。身份来自持久化 ExecutionSpec，不能由模型输出覆盖；模型返回的相同字段必须核对。所有路径按框架生成的位置解析并检查范围，不能盲信模型返回的任意本机路径。

实现输出包含 summary、claimed_acceptance 和 limitations；这些仅是声明，框架另行检查代码与验收证据。无代码变化时可以使用已固定基线 SHA 作为 no-op 候选，但仍须执行全部必要检查和独立评审，并明确记录无新提交。

评审输出包含 verdict、findings（验收项、严重程度、文件位置和理由）、limitations；verdict 只允许 pass、request_changes、unable_to_review。仅 pass 且必要证据齐全才能交付；request_changes 进入有限修复流程；unable_to_review 不视为通过或已完成的修复，按明确环境问题或待人工处理，不能反复重试绕过上限。格式错误和身份/版本不匹配是无效执行结果。

能力与权限先做硬筛选，再按配置后端顺序及可用容量选择；首版不声称具备自动质量预测或最优价格路由。评审后端也走相同协议，强制使用全新会话，不 resume 实现会话。

替换后端建立新的尝试；须先确认旧执行停止或隔离其写入影响。仅传递明确保存的代码和上下文，不假定会话、机器文件或权限可以透明迁移。未来 SSH/HTTP 后端可以沿用协议，但必须如实声明幂等、状态查询及取消限制。

## 8. 本地 Codex 适配

已在当前环境核实 `codex-cli 0.171.0` 的 `codex exec --help`；支持 stdin prompt、`--json`、`--output-schema`、`--output-last-message`、`--model`、`--cd` 和 sandbox 选项。非交互与结构化输出的用途另见 [Codex 官方文档](https://learn.chatgpt.com/docs/non-interactive-mode)。

拟议调用形态为 argv 数组构造的 `codex --ask-for-approval never exec --cd <workspace> --json --output-schema <schema> --output-last-message <result> --sandbox <mode> -`，prompt 通过 stdin 传入。实现使用 workspace-write，评审使用 read-only；模型 ID 配置后传入，不把“codex”当作具体模型名。全局审批参数与 exec 子命令组合已用本机 help 解析验证，但不等于真实执行 smoke test 通过。

runner 保存原始 JSONL、stderr、退出码和最终响应；适配器解析已知事件，忽略可扩展的未知事件类型，遇到无效结果则失败而非视为通过。退出码为零不代表任务完成。最终输出 schema、实际 Git 版本、检查结果和评审结论共同决定下一步。

每次实现、修复和评审均创建新会话，不使用 `resume --last`。session ID 若事件提供则保存；若缺失不伪造，评审会话独立性仍由新建进程和无 resume 调用约束。

认证沿用本机已配置的 Codex 登录，不复制登录文件到任务卡或运行状态。启动前 doctor 检查可执行程序、必要 flags、配置、仓库及可用能力；实际登录及调用是否可用须通过后续 smoke test 验证。本次设计阶段没有启动真实任务或验证认证。

无人值守调用显式采用 never 审批策略：在所选沙箱允许范围内执行，不能通过交互提权。缺少必要权限、工具或网络时标记 needs_attention，不能靠无限重试解决；此类问题的实际事件和错误映射在 smoke test 中核实。自动化不得添加 bypass sandbox 或自动信任项目的参数。

worktree 共享 Git 元数据，workspace-write 不是针对恶意代码的强隔离。首版仅用于作者信任的仓库和本机环境，不宣称可以安全执行任意来源任务。调度状态目录不能通过 add-dir 赋予 Agent 写权限；框架检查异常元数据变更并停止交付。

## 9. Git 产物与依赖集成

首次派发准备工作区时，将 base_ref 解析为不可变 base SHA 并持久化；显式提交 SHA 在提交时就固定。冻结后同一任务的重试、修复及恢复沿用该基线，不跟随分支漂移。首版不自动 fetch，分支的“当前”指已注册本地仓库的状态；交付不承诺已与最新主分支完成集成。拒绝直接操作用户脏工作区，每任务创建专用 worktree 和框架命名空间下的分支/产物 ref。

准备依赖任务时，从自身 base SHA 创建集成工作区，按确定的拓扑顺序合并前置不可变产物提交。保留提交祖先关系，避免钻石依赖被重复 cherry-pick；已包含的祖先不重复合并。合并提交只存在于任务分支，不合并目标主分支。

集成开始前保存确定的合并计划（有序前置产物与 SHA）、当前游标、已集成 SHA、每一步起止提交和当前冲突步骤。冲突保留现场、日志和相关 SHA，当前准备尝试确认停止后释放槽位，任务进入 needs_attention，人工处理期间不继续推进。

解除冲突需用户在该任务工作区完成处理，再调用 resolve-conflict；框架核对 generation、预期父提交、依赖清单和未合并索引，拒绝不匹配现场。校验成功后保存本步本地集成提交，更新游标；重新获得实现额度后继续合并剩余前置产物。只有全部计划完成、所有前置 head SHA 均为最终集成提交祖先，才能固定集成基线并开始编码，之后仍需完整验证和独立评审。普通 retry 不重置冲突现场，禁止自动丢弃用户改动。

Agent 实现结束后框架检查修改路径和差异，生成本地提交，固定候选 SHA。验证在该候选版本上执行；验证后再次核对提交、索引、工作区和未跟踪文件。测试产生的非忽略改动不能静默混进已验证代码。

评审使用单独的只读 worktree，固定相同候选 SHA，输入包括任务约定、依赖清单、变更、验证报告和全局/项目规则快照。评审差异基于依赖集成后的基线，避免将前置任务变更全部算作当前任务修改。

通过后生成不可变产物清单，绑定约定版本、基线、候选 SHA、检查和评审。下游读取该清单并确认 Git 对象存在。必要检查失败、证据缺失或版本变化均不能交付。

交付发布顺序：先结束所有会修改产物的进程；将不可重建的证据写入临时文件、flush/fsync 后在同一文件系统原子发布，清单记录摘要；验证文件可读、摘要和 Git 对象有效；最后以一个数据库事务登记产物、切换 delivered 并追加事件。目录 fsync 等持久性能力按运行平台实现并在 doctor 中检查，不宣称原子 rename 本身等于断电持久化。数据库记录清单与必要元数据，文件保存日志和响应。

先发布文件后数据库提交失败时，文件成为孤立对象，不解锁依赖；启动核对时记录并延后清理。数据库已交付但文件在迁移、磁盘故障或人工操作后缺失时，设置 artifact_unavailable 阻塞下游，不静默复用不完整证据；恢复文件后核对摘要再解除阻塞。文件损坏的已交付产物不能被新结果原地覆盖。

必要测试明确因代码/验收不满足而失败，或评审给出 request_changes 时，共享最多两轮自动修复预算。初次实现不计修复轮次；新修复尝试获得派发许可时以事务将 repair_round 加一，失败后重试同一轮使用同一轮次及有限基础设施重试额度，不能反复创建尝试重置预算。每轮携带测试日志或评审意见，重新生成候选版本、验证及独立评审。两轮耗尽后仍不满足则进入 needs_attention。

测试无法运行、依赖或权限缺失、评审输出损坏和进程故障不应冒充验收失败；按明确的错误分类处理，无法可靠分类则待人工核对。此类故障不自行开新修复轮，但同样受有限重试限制。显式 task retry 保留 repair_round；耗尽修复预算后不能靠 retry 获得额外自动修复次数，变更约定需要按已确认策略重建任务。

## 10. 崩溃、取消与重试

每个 runner 使用独立进程组，先保存身份记录（尝试 ID、PID、进程开始标识）再启动 Codex，退出后以原子替换方式写 exit.json。PID 不能单独作为存活依据，防止 PID 复用误杀。

数据库预留、进程启动、文件写入不能形成一个原子事务，必须正视不确定窗口：

| 故障位置 | 恢复行为 |
| --- | --- |
| 预留后明确未启动 | 标记本次失败、释放容量，可有限重试 |
| 启动响应丢失或身份记录不足 | 保持 unknown 与额度，不自动重发，转人工核对 |
| 调度器退出但 runner 活着 | 新调度器读取身份与完成文件，继续跟踪，不启动重复尝试 |
| runner/Codex 已退出且结果完整 | 幂等接收结果，执行正常版本校验 |
| 退出但无完整结果 | 保留现场，确认无残余写入进程后才能重试 |
| 磁盘或 SQLite 写入失败 | 停止新派发，保留未知占用，不错误宣告成功 |

取消：先提交 cancel_requested 并禁止新尝试，通知 runner 停止进程组；停止确认后取消尝试、释放额度并标记 cancelled。先请求温和终止，超时再强制停止；仍无法核实时保留 unknown。迟到结果存档，但不能解锁依赖。

runner 在启动前核对尝试与 generation，调度器在预留后不能仅依赖旧的内存状态启动进程。取消与启动仍存在跨进程窗口：取消意图持久化保留，直到 supervisor 明确确认本次尝试未启动或进程组已停止才释放额度。若取消恰好先于子进程创建，runner 必须在启动后再次检查并停止子进程；不得把尚在 starting 的尝试直接视为已取消完毕。

初版崩溃窗口不承诺 exactly-once 外部执行，采用“不确定时不重复派发”。重试退避拟议默认 30 秒起、最多自动重试 2 次、上限 5 分钟；仅对明确已停止的暂时性故障自动重试。认证、权限、格式错误及冲突转人工。超时在配置中必填合理值，不以无限等待掩盖失败。

## 11. 时段、可观测性与迁移

时段策略使用配置时区和半开区间，明确夏令时与跨午夜的处理；比较时转换为 UTC 时间点，持久化记录 UTC。时区数据不可用时拒绝启动该策略。实现选择需兼容 Python 3.9 的 zoneinfo 能力。

窗口只限制新派发，不强杀运行中任务。实现与评审可以配置不同窗口，紧急任务只有显式允许时才跳过窗口。首版使用人工配置的时间窗口，不自动抓取供应商价格；价格与实际用量不齐时显示未知成本，不根据 token 数编造账单。

事件记录含调度原因、等待时间、后端、轮次和状态转换。提供按任务和尝试查询的日志位置。日志可能含源码，默认本机私有保存；清理必须显式，不删除仍被依赖引用的产物。

迁移要求停止调度、确认全部执行停止、备份数据库和产物，重建或重新注册目标机 Git 仓库与 worktree，并重新配置后端、认证和路径。不复制 Codex 登录凭据，不宣称运行中任务可以热迁移。

## 12. 实现顺序与验收

1. contracts、SQLite 存储、任务卡 CLI 和 mock 后端。
2. 调度及容量事务、DAG、排序、移出和稳定事件。
3. runner、身份核对、取消、崩溃故障注入。
4. worktree、依赖集成、验证与不可变产物。
5. Codex 实现与独立评审、两轮返工及人工恢复。
6. 时段、迁移演练及持续运行实验。

必要自动化测试聚焦不变量：并发与资源池上限、重复提交、DAG/钻石集成、插队、取消竞态、崩溃窗口、过期结果、候选版本变更、返工上限和时段边界。测试使用 mock 后端和可控时钟，真实 Codex 测试单独显式运行，不让普通测试消耗模型额度。

演示任务覆盖：两个独立任务并行；依赖任务等待正确产物；紧急插队；移出前置后下游阻塞；独立评审要求返工；调度器重启不重复派发。持续运行记录实际吞吐、失败率、等待时间和可获得的费用，不预填提升数字。

## 13. Review 记录

用户已确认的选择见第 1 节。[独立评审报告](reviews/tech-design-v0.1-review.md)保留初审发现及后续复审记录。已补充取消启动握手、验证占槽、人工恢复、证据发布顺序、结果契约和依赖冲突集成游标。测试失败预算、基线冻结及任务链变更策略已由作者确认并写入本设计。独立最终复核确认 R1–R9 均在设计层关闭，当前无必须继续询问的产品问题，可以交作者最终 review；运行可靠性仍需实现后的测试验证。
