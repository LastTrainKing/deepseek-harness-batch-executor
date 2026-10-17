# deepseek-harness-batch-executor

> **简体中文**：面向 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (`dsh`) 的高效执行 Skill——让**模型只参与真正需要判断的步骤**，把确定性工作合并为一次脚本化执行（程序化工具调用 PTC 纪律），在汇报前**本地核验**结果（数量、sha256 树哈希、差异），并对无效重试**及时止损**。
>
> - 与 Harness 无关：**Python 3.8+ 纯标准库**，零依赖、不联网。
> - 作为 DSH 原生 Skill 安装（`SKILL.md` + 辅助脚本），也可在任何能运行 Python 的 Agent 环境中直接使用。
>
> 📖 完整中文文档：[README.zh-CN.md](README.zh-CN.md)

---

Efficient tool execution for [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (`dsh`): a skill that keeps the **model in the loop only for steps that need judgment**, moves deterministic work into one scripted pass (the Programmatic Tool Calling discipline), verifies results **locally** before reporting, and **stops unproductive retry loops** instead of burning tokens on them.

- Harness-agnostic scripts: **Python 3.8+, standard library only** — no pip install, no network.
- Installs as a native DeepSeek Harness **skill** (`SKILL.md` + helper scripts), and works in any harness that can run Python.

---

## Why: the three cost centers of long agent sessions

In long-running harness tasks the bill comes from three places:

1. **Repeated context cost** — every model turn re-carries the conversation: past messages, past tool calls, past tool *results*. Mechanical steps (copy this file, check that file, count those lines) each trigger a full round trip, and each round trip pays for the whole transcript again.
2. **Round-trip inflation** — a 20-file copy done step-by-step is ~40 model round trips; the model is present for steps that need no judgment.
3. **Verbose feedback** — raw tool output floods the context (hundreds of search hits, full test logs, whole-file dumps) even though the model only needs a status, a count, a diff, or an exception summary.

This project's answer, per cost center:

| Problem | Mechanism | Where |
|---|---|---|
| Mechanical steps trigger repeated model round trips | **Plan → batch execute → receipt**: one programmatic pass for the whole deterministic sequence | `scripts/batch_exec.py`, SKILL.md Layer 2 |
| Tool output floods the context | **Compressed feedback**: summary to stdout, full detail to a local log with stable ids, lazy on-demand deep reads | `scripts/search_summary.py`, `scripts/test_summary.py` |
| "Did my edit land?" stays unverified | **Local verification**: counts, sha256 tree hashes, diffs — evidence, not vibes | `scripts/verify_state.py`, plan `verify` section |
| Blind retries and repeated no-op edits | **Loop governance**: evidence hashes detect no-progress; retry limits stop the loop with a reason | `scripts/loop_guard.py` |
| Destructive ops run without scrutiny | **Approval gates**: remove/overwrite require explicit approval; dry-run first | `batch_exec.py` `--approve` / `--dry-run` |

## Architecture & workflow

```
            ┌──────────────────────────── model (judgment) ───────────────────────────┐
            │ 1. CLASSIFY  split task: deterministic (batchable) vs judgment-required │
            │ 2. PLAN      write plan JSON (or one program, if native PTC is on)      │
            └───────────────────────────────────┬──────────────────────────────────────┘
                                                │ one hand-off
            ┌──────────────────────────── executor (deterministic) ───────────────────┐
            │ 3. PRE-CHECK  sources, destinations, policies                           │
            │ 4. EXECUTE    the whole planned sequence, continuously                  │
            │ 5. VERIFY     counts, hashes, diffs — locally, cheap-first              │
            └───────────────────────────────────┬──────────────────────────────────────┘
                                                │ one receipt + evidence pointers
            ┌──────────────────────────── model (judgment) ───────────────────────────┐
            │ 6. REPORT     compact receipt only; deep reads are lazy (by log path)   │
            │ 7. GUARD      loop_guard check/record before every retry; honor STOP    │
            └─────────────────────────────────────────────────────────────────────────┘
```

Native PTC note: DeepSeek Harness's Code Mode / programmatic tool calling does exactly this natively when enabled — **use it when available**. When it is not enabled, the same shape is achieved with the harness's ordinary file-write + command-execution tools (write the plan/script, run it, read only the summary). The skill documents both paths and the decision rules for each.

## Installation

### As a DeepSeek Harness plugin (recommended)

The repository ships a `dsh.bundle` manifest (`package.json` + `cordis.patch.yml`), so it installs as a native plugin and the skill is registered automatically — no manual copying:

```bash
dsh plugin add github:LastTrainKing/deepseek-harness-batch-executor
```

The plugin registers the bundled `SKILL.md` through the `ctx.skills` provider seam; the helper scripts under `scripts/` ship inside the package and resolve relative to the skill directory. It is listed on the [awesome-dsh-plugin](https://github.com/awesome-dsh-plugin/awesome-dsh-plugin) directory (category `skill`).

### As a plain skill (clone into the skill directories)

If you prefer not to use the plugin channel, DSH also scans skill directories directly (project-level wins over user-level):

```bash
# user-level (available in every project)
git clone https://github.com/LastTrainKing/deepseek-harness-batch-executor.git \
    ~/.dsh/skills/deepseek-harness-batch-executor

# or project-level (recommended for teams: commit the skill with the repo)
git clone https://github.com/LastTrainKing/deepseek-harness-batch-executor.git \
    <your-project>/.dsh/skills/deepseek-harness-batch-executor
```

Equivalent locations that DSH also scans: `<project>/.agents/skills/` and `~/.agents/skills/`. Custom directories can be registered via `~/.dsh/settings.yaml` (`skill-explorer.customSkillDirs`) or the `DSH_CUSTOM_SKILL_DIRS` environment variable. Skills are hot-reloaded — no restart needed.

### As plain scripts (any harness / CI)

```bash
git clone https://github.com/LastTrainKing/deepseek-harness-batch-executor.git
cd deepseek-harness-batch-executor
python scripts/batch_exec.py --plan examples/sample-plan.json   # try it
```

## Usage

### 1. Batch execution with verification — `scripts/batch_exec.py`

```bash
python scripts/batch_exec.py --plan plan.json [--dry-run] [--approve] \
                             [--log-dir DIR] [--fail-fast] [--json]
```

`plan.json` (see [`examples/sample-plan.json`](examples/sample-plan.json)):

```json
{
  "plan_id": "copy-configs",
  "overwrite": "if-different",
  "operations": [
    {"id": "op-1", "op": "copy", "src": "templates/a.conf", "dst": "build/a.conf"},
    {"id": "op-2", "op": "mkdir", "path": "build/out"},
    {"id": "op-3", "op": "expect", "path": "build/a.conf", "expect": {"exists": true}},
    {"id": "op-4", "op": "remove", "path": "build/tmp", "requires_approval": true}
  ],
  "verify": [
    {"type": "count", "pattern": "build/*.conf", "expect": 8},
    {"type": "tree-hash", "src": "templates", "dst": "build"}
  ]
}
```

Sample receipt (real output from running `examples/sample-plan.json` against 8 template files):

```
[receipt] plan=copy-configs status=success elapsed_ms=56
counts: total=10 ok=10 already_satisfied=0 failed=0 refused=0 planned=0
operations:
  op-1                     mkdir    ok                 created directory: build/configs
  op-2                     copy     ok                 copied templates/app.conf -> build/configs/app.conf (14 bytes)
  ... (op-3 .. op-9: copies, all ok)
  op-10                    expect   ok                 expectation satisfied: build/configs/db.conf
verify:
  count        pass  glob 'build/configs/*.conf' matched 8 file(s), expected 8
  tree-hash    pass  trees identical (8 file(s))
full log: .dsh-batch-logs/copy-configs-20261017T043700Z.json
```

Key properties: idempotent (`if-different` reports `already_satisfied` instead of re-copying), verification is content-hash based (not "the command returned 0"), every run writes a full JSON audit log, and exit codes are explicit (`0` success, `2` partial failure/refusal, `1` fatal).

### 2. Search with compressed feedback — `scripts/search_summary.py`

```bash
python scripts/search_summary.py --root . --pattern "ERROR|WARN" \
    --include "*.log" --top 5
# -> total_matches=147 files_touched=12 + per-file counts + 5 representative hits
# -> full results: .dsh-batch-logs/search-<ts>.jsonl (147 entries, stable ids)

python scripts/search_summary.py --show-id m-0041 --log-file <log.jsonl>  # lazy deep read
```

### 3. Post-modification verification — `scripts/verify_state.py`

```bash
python scripts/verify_state.py snapshot --paths README.md src/ --out expected.json
# ... make your edits ...
python scripts/verify_state.py check --expect expected.json
# -> PASS/FAIL per path with sha256 evidence (expected vs actual)

python scripts/verify_state.py diff --a old.txt --b new.txt
# -> +12 / -3 lines, first hunks, full diff in .dsh-batch-logs/diff-<ts>.txt
```

### 4. Test / command feedback — `scripts/test_summary.py`

```bash
python scripts/test_summary.py -- python -m pytest tests/ -q
# -> exit code, counts (pytest/jest/go formats recognized), bounded failure
#    list, full log path; failure details never discarded

python scripts/test_summary.py --show-failure 3 --log-file <run.log>   # deep read
```

### 5. Retry governance — `scripts/loop_guard.py`

```bash
python scripts/loop_guard.py check  --action "fix:src/db.py:line42"
# -> PROCEED / WARN / STOP (+ reason)

python scripts/loop_guard.py record --action "fix:src/db.py:line42" \
    --result fail --evidence-file src/db.py
```

STOP is returned when the retry limit is reached, or when two consecutive attempts share the same evidence hash (the world did not change — retrying is a loop). The skill instructs the agent to **honor STOP and report the reason** instead of retrying.

## Good fit / bad fit

**Good fit**

- Batch copy/move/cleanup of many files with content verification
- Searching large trees or logs where only counts + samples matter
- Long sessions where transcript growth dominates cost
- Repetitive mechanical steps after the model has decided the plan
- Any task where "command returned 0" is not proof the goal was achieved

**Bad fit**

- One-shot trivial operations (one `cp`, one small-file read) — planning costs more than it saves
- Steps requiring judgment at every point (design decisions, ambiguous requirements)
- Interactive/streaming steps where a batch would hide required approval prompts
- Destructive operations you cannot review — always dry-run and confirm first

## Safety & permissions

- **Approvals are never bypassed.** `remove` operations require `requires_approval: true` in the plan **and** `--approve` on the command line; without both, the operation is refused (exit 2) and nothing is deleted. `overwrite: never` refuses to clobber existing files.
- **The harness's own permission prompts stay in force.** Batching reduces model round trips; it does not reduce or skip authorization. Destructive steps should be surfaced to the user through the harness's normal approval flow.
- **Dry-run first** for any destructive plan: `--dry-run` reports what would happen without touching anything.
- **Bounded execution**: explicit operation lists, exit codes, and a timeout (`test_summary.py --timeout`) — no unbounded loops, no infinite retries.
- **Logs are the audit trail**: every batch/search/run writes a full log; keep them, cite them, and scrub secrets before sharing.
- **No secrets in plans or logs**: never write credentials, tokens, or personal data into plan files.

## Compatibility & known limitations

| Item | Status |
|---|---|
| Python | 3.8+, standard library only (developed & tested on 3.13) |
| Platforms | Windows (Git Bash), Linux, macOS — `os.path` semantics throughout; verified on Windows in this project's test runs |
| DeepSeek Harness | Skill format follows the DSH skill spec (`SKILL.md` frontmatter + `<skill_instructions>` / `<skill_resources>`); tested for metadata compliance, not against a live `dsh` runtime |
| Native PTC / Code Mode | Not required; when DSH Code Mode is enabled, prefer it — the skill documents both paths |
| pytest/jest/go parsing | Best-effort format recognition; unknown formats fall back to labeled candidate error lines — never invented counts |
| Permission-denied scenarios | Tested via real filesystem errors (missing sources, collisions) and the approval flag; `chmod`-based tests are not portable and were not used |
| Model token measurement | **Not measured in this project** — see Performance section |

## Testing

```bash
python tests/run_tests.py        # 22 tests, stdlib unittest, ~7s
node _selftest.mjs               # plugin smoke test (mocked ctx.skills seam)
```

Every test runs the real scripts as subprocesses against real fixtures in a temp directory. Scenarios:

| # | Scenario | Result |
|---|---|---|
| T1 | Batch copy 20 files + count & tree-hash verification | pass |
| T2 | Idempotent re-run (uncertain result → state check, no duplicate work) | pass |
| T3 | Batch search: bounded summary, full JSONL log, on-demand deep read, long-line truncation | pass |
| T4 | Modify a file → `check` fails with sha256 evidence → `diff` shows exactly what changed | pass |
| T5 | Failing command → failure summary extracted, full log kept, `--show-failure` deep read; pytest/go parser unit tests; no invented counts | pass |
| T6 | Partial failure (missing source): exit 2, `status=partial`, failing op named; `--fail-fast` marks skipped ops | pass |
| T7 | `remove` refused without `--approve`; executed with it; refused when plan omits `requires_approval`; `overwrite: never` conflict | pass |
| T8 | Loop guard: identical-evidence repeat failure → STOP; retry limit → STOP; success-without-change → WARN; status/reset | pass |
| T9 | SKILL.md metadata compliance with the DSH skill spec (name regex, required fields, section tags, repo layout) | pass |
| P1 | Plugin registration: mock `ctx.skills`, assert the bundled SKILL.md is discovered (list), its body is served (get), and an aborted lookup rejects | pass |

**Actual run (2026-10-17, Python 3.13.14 / Node 22, Windows 10): `Ran 22 tests ... OK` and `selftest ok`.** Uncovered: behavior inside a live `dsh` runtime (no dsh instance available in the development environment — the plugin targets the `@deepseek-ai/dsh-skill` ^0.2.0-rc.2 provider seam, the same seam other listed skill packs target), token accounting, and network/permission models beyond the local filesystem.

## Performance

### What was measured (real numbers, this project)

Controlled comparison, identical task and fixtures: copy 100 files (28 KB total) and verify content consistency. Reproduce with:

```bash
python examples/bench_naive_vs_batched.py --files 100 --iterations 3
```

| Metric | Naive (step-by-step) | Batched | Measured on |
|---|---:|---:|---|
| Avg command executions | 200 | 1 | 2026-10-17, Python 3.13.14, Windows |
| Avg wall-clock time | 18.41 s | 0.45 s | same |
| Verification | none (command exit codes only) | count + full tree sha256 | same |
| Audit log | none | full JSON per run | same |

"Command executions" maps 1:1 to model tool calls in the naive pattern (one copy + one verify command per file). A second, conversation-level measurement in a live agent session (copying 4 files): naive pattern = 4 tool calls, no verification; batched pattern = 2 tool calls (write plan + execute) with both verifications passing.

### What was NOT measured (and must not be fabricated)

- **Model input/output tokens per request.** Token usage depends on how the harness serializes conversation history into each request; it cannot be derived from character counts. The batched receipt above is ~23 KB of text for 100 operations — that is a *payload size*, not a token count.
- **DSH-internal overheads** (message serialization, state management).

Measurement protocol for the target harness (DSH): run the same task twice (naive vs batched) with identical initial data and model configuration; export the session logs (DSH keeps append-only session logs, and recent RC versions expose per-turn token/timing statistics); sum input tokens, output tokens, tool-call counts, and elapsed time; report side by side. See [`references/ptc-playbook.md`](references/ptc-playbook.md) §7.

### External reference data (NOT this project's results)

- Published DSH PTC experiment (log-statistics task): 66,496 → 55,043 total tokens (−17%), tool calls 4 → 1. Source: Tencent Cloud developer community, "拆解DeepSeek Harness(四):PTC模式" (2026-08).
- Anthropic, "Code execution with MCP" (2025-11): 150k → 2k tokens (−98.7%) in their example via code-orchestrated tool calls with in-sandbox filtering.

Token savings are task-dependent: large when many tool results would otherwise flow through the model, near zero (or negative) when a single native tool call suffices. Do not quote a fixed percentage as this project's result.

## Contributing

Issues and PRs welcome. Please:

- keep the scripts stdlib-only and Python 3.8+ compatible;
- add a test in `tests/run_tests.py` for every behavioral change;
- keep SKILL.md aligned with the actual script interfaces;
- never weaken the approval gates or the verification defaults.

## License

[MIT](LICENSE).
