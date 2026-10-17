# deepseek-harness-batch-executor（中文说明）

面向 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)（`dsh`）的高效工具执行 Skill：
**模型只负责决策，执行器负责批量操作，验证机制负责确认结果，循环守卫负责在任务缺乏进展时及时停止。**

核心理念不是“少调几次工具”，而是把长任务中的三类成本分别治理：

1. **重复上下文成本** —— 每次模型往返都重新携带全部历史消息、工具调用与工具返回；机械步骤（复制这个文件、检查那个文件、数一下这些行）每一步都触发一次完整往返。
2. **往返膨胀** —— 20 个文件的逐步复制 ≈ 40 次模型往返，而模型本不需要参与这些步骤。
3. **工具反馈冗余** —— 原始工具输出（成百上千条搜索命中、完整测试日志、整文件转储）涌入上下文，而模型真正需要的只是状态、数量、差异和异常摘要。

本项目把上述理念落成 Agent 可实际遵循的 Skill 规范与五个可独立运行的辅助脚本。

## 机制与脚本对应关系

| 问题 | 机制 | 实现 |
|---|---|---|
| 机械步骤造成重复往返 | **计划 → 批量执行 → 回执**：一次程序化执行完成整段确定性序列 | `scripts/batch_exec.py`（SKILL.md 第 2 层） |
| 工具输出淹没上下文 | **分级反馈**：摘要进上下文，完整结果落本地日志（带稳定 ID），按需深读 | `scripts/search_summary.py`、`scripts/test_summary.py`（第 3 层） |
| 修改是否生效无从确认 | **本地核验**：数量、sha256 树哈希、差异对比——用证据说话 | `scripts/verify_state.py` 与计划的 `verify` 段 |
| 盲目重试与无效修改 | **循环治理**：证据哈希检测“无变化即无进展”，重试上限到时 STOP 并给出原因 | `scripts/loop_guard.py`（第 4 层） |
| 高风险操作失控 | **审批门**：删除/覆盖必须显式批准，先 dry-run | `batch_exec.py --approve / --dry-run` |

## 架构与流程

```
 模型（判断）                执行器（确定）                 模型（判断）
 ─────────                  ───────────                   ─────────
 1. 分类 ────────────────┐
 2. 制定计划（JSON）─────┼──▶ 3. 预检（源/目标/策略）
                          │    4. 批量执行（连续、有界）
                          │    5. 本地核验（先廉价后深入）
                          └──▶ 6. 汇报（紧凑回执 + 证据指针）
                               7. 守卫（重试前 check，STOP 即停）
```

原生 PTC 说明：DeepSeek Harness 的 Code Mode / 程序化工具调用启用时，优先用原生机制（生成一段程序完成同样的预检-执行-核验-返回）。未启用时，用 Harness 普通的文件写入 + 命令执行工具实现同等形态（写计划/脚本 → 执行 → 只读摘要）。SKILL.md 对两条路径均给出决策规则。

## 安装

### 作为 DeepSeek Harness 插件安装（推荐）

仓库自带 `dsh.bundle` 清单（`package.json` + `cordis.patch.yml`），可作为原生插件安装，技能自动注册，无需手工拷贝：

```bash
dsh plugin add github:LastTrainKing/deepseek-harness-batch-executor
```

插件通过 `ctx.skills` 提供者接缝注册内置的 `SKILL.md`；`scripts/` 下的辅助脚本随包分发，按技能目录相对路径解析。本项目已收录于 [awesome-dsh-plugin](https://github.com/awesome-dsh-plugin/awesome-dsh-plugin) 插件目录（分类 `skill`）。

### 作为普通技能克隆（目录扫描安装）

不想走插件通道时，DSH 也会直接扫描技能目录（项目级优先于用户级）：

```bash
# 用户级（所有项目可用）
git clone https://github.com/LastTrainKing/deepseek-harness-batch-executor.git \
    ~/.dsh/skills/deepseek-harness-batch-executor

# 项目级（推荐：随项目仓库一起提交）
git clone https://github.com/LastTrainKing/deepseek-harness-batch-executor.git \
    <项目>/.dsh/skills/deepseek-harness-batch-executor
```

DSH 同时扫描 `<project>/.agents/skills/` 与 `~/.agents/skills/`；自定义目录可通过 `~/.dsh/settings.yaml` 的 `skill-explorer.customSkillDirs` 或环境变量 `DSH_CUSTOM_SKILL_DIRS` 注册。Skill 支持热加载，无需重启。

仅作为普通脚本使用（任意 Harness / CI）：`python scripts/batch_exec.py --plan examples/sample-plan.json`。

## 使用示例

```bash
# 1) 批量执行 + 核验
python scripts/batch_exec.py --plan plan.json [--dry-run] [--approve]

# 2) 批量搜索（只回摘要，完整结果落 JSONL 日志）
python scripts/search_summary.py --root . --pattern "ERROR|WARN" --top 5
python scripts/search_summary.py --show-id m-0041 --log-file <log.jsonl>   # 按需深读

# 3) 修改后核验
python scripts/verify_state.py snapshot --paths README.md src/ --out expected.json
python scripts/verify_state.py check --expect expected.json
python scripts/verify_state.py diff --a old.txt --b new.txt

# 4) 测试/命令输出压缩（完整日志不丢，--show-failure 按需读取）
python scripts/test_summary.py -- python -m pytest tests/ -q

# 5) 重试治理（STOP 必须遵守并如实上报原因）
python scripts/loop_guard.py check  --action "fix:src/db.py:line42"
python scripts/loop_guard.py record --action "fix:src/db.py:line42" --result fail \
    --evidence-file src/db.py
```

完整用法、计划 JSON 格式与真实回执示例见英文 [README.md](README.md)。

## 适用与不适用

- **适用**：多文件批量复制/移动/清理并需内容核验；大树/大日志搜索（只要计数与样本）；长会话中转录增长主导成本的场景；模型已决策后的重复机械步骤；凡是“命令返回 0”不足以证明目标达成的任务。
- **不适用**：一次性琐碎操作（单个 `cp`、读小文件）——计划成本高于收益；每步都需要判断的设计/需求类步骤；需要流式或交互反馈、批量会掩盖必要审批的步骤；无法先行 dry-run 确认的高危操作。

## 安全机制

- **绝不绕过审批**：`remove` 必须同时满足计划中 `requires_approval: true` 与命令行 `--approve`，否则拒绝执行（退出码 2，文件不动）；`overwrite: never` 拒绝覆盖已存在文件。
- **Harness 自身权限提示依然生效**：批处理减少的是模型往返，不是授权。
- **破坏性操作先 dry-run**，再人工确认。
- **有界执行**：显式操作列表、显式退出码、命令超时；无无限循环、无无限重试。
- **日志即审计轨迹**：每次批量/搜索/命令运行都写完整日志；不外泄含密钥、令牌、个人数据的日志。

## 兼容性与已知限制

- Python 3.8+，仅标准库（开发与测试环境：Python 3.13.14，Windows 10 + Git Bash；`os.path` 语义，Linux/macOS 行为一致）。
- SKILL.md 遵循 DSH Skill 规范（frontmatter + `<skill_instructions>`/`<skill_resources>`）；已做元数据合规测试，但未在真实运行的 `dsh` 实例中验证（开发环境无 dsh）。
- pytest/jest/go 格式识别为尽力而为；未识别格式回退为“候选错误行”并明确标注，绝不编造计数。
- 权限不足场景以真实文件系统错误（源缺失、目标冲突）与审批标志模拟；未使用不可移植的 `chmod` 测试。
- **模型 Token 未实测**：本环境无法取得每次请求的真实 Token 统计，详见性能一节。

## 测试

```bash
python tests/run_tests.py    # 22 个用例，标准库 unittest，约 7 秒
node _selftest.mjs           # 插件冒烟测试（mock ctx.skills 接缝）
```

覆盖：批量复制+数量与内容核验（T1）、幂等重跑（T2）、批量搜索摘要+完整日志+按需深读+长行截断（T3）、修改后哈希证据核验+差异（T4）、测试失败摘要提取+完整日志+深读+解析器单测+不编造计数（T5）、部分失败不得谎报成功+fail-fast（T6）、删除审批门+覆盖冲突（T7）、循环守卫 STOP/WARN/上限/状态（T8）、SKILL.md 元数据与目录合规（T9）、插件注册（mock `ctx.skills`，验证 SKILL.md 被发现、正文被完整返回、中止信号被遵守）（P1）。

**实际运行结果（2026-10-17，Python 3.13.14 / Node 22，Windows）：`Ran 22 tests ... OK` 与 `selftest ok`（全部通过）。** 未覆盖：真实 `dsh` 运行时内的行为（开发环境无 dsh 实例；插件目标接缝为 `@deepseek-ai/dsh-skill` ^0.2.0-rc.2 提供者接缝，与已收录技能包插件一致）、Token 计量、本地文件系统之外的权限模型。

## 性能

### 本项目实测（可复现）

相同任务、相同初始数据：复制 100 个文件（共 28 KB）并核验内容一致性。复现命令：
`python examples/bench_naive_vs_batched.py --files 100 --iterations 3`

| 指标 | 逐步调用（naive） | 批量执行（batched） | 测量条件 |
|---|---:|---:|---|
| 平均命令执行次数 | 200 | 1 | 2026-10-17，Python 3.13.14，Windows |
| 平均耗时 | 18.41 秒 | 0.45 秒 | 同上 |
| 核验 | 无（仅命令退出码） | 数量 + 全树 sha256 | 同上 |
| 审计日志 | 无 | 每次运行完整 JSON | 同上 |

“命令执行次数”与 naive 模式下模型工具调用一一对应（每文件一次复制 + 一次校验命令）。另有一次真实会话级实测（复制 4 个文件）：naive = 4 次工具调用、无核验；batched = 2 次工具调用（写计划 + 执行）、两项核验全部通过。

### 未实测（不得编造）

- **每次请求的模型输入/输出 Token**：取决于 Harness 如何把会话历史序列化进请求，无法由字符数推算。batched 回执约 23 KB（100 个操作）——这是**载荷体积**，不是 Token 数。
- **DSH 内部开销**（消息序列化、状态管理等）。

在 DSH 上的测量协议：相同任务跑两遍（naive vs batched，相同初始数据与模型配置）→ 导出会话日志（DSH 为仅追加会话日志，近期 RC 版本提供每回合 Token/耗时统计）→ 分别汇总输入 Token、输出 Token、工具调用次数、耗时 → 并排报告。详见 `references/ptc-playbook.md` 第 7 节。

### 外部参考数据（非本项目实测结论）

- 已发表的 DSH PTC 实验（日志统计任务）：66,496 → 55,043 总 Token（−17%），工具调用 4 → 1 次。来源：腾讯云开发者社区《拆解DeepSeek Harness(四):PTC模式》（2026-08）。
- Anthropic《Code execution with MCP》（2025-11）：其示例中 150k → 2k Token（−98.7%），代码编排工具调用 + 沙箱内过滤。

Token 收益与任务强相关：多个工具结果本会流经模型时收益大；单个原生工具调用即可完成时接近零甚至为负。请勿把任何固定比例当作本项目实测结果。

## 贡献

欢迎 Issue 与 PR。要求：脚本保持纯标准库、Python 3.8+ 兼容；每个行为变更附带 `tests/run_tests.py` 测试用例；SKILL.md 与实际脚本接口保持同步；不得削弱审批门与核验默认值。

## 许可证

[MIT](LICENSE)。
