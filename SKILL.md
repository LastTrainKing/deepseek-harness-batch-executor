---
name: deepseek-harness-batch-executor
description: "Efficient tool execution for DeepSeek Harness: batches deterministic file operations into one scripted pass, compresses verbose tool output into summaries with on-demand deep reads, verifies results locally before reporting, and stops unproductive retry loops."
whenToUse: "When a task needs many repetitive tool operations (batch copy/move/search/cleanup of files), when tool output would be large (log files, search hits, test or build runs), after modifying files when the effect must be verified, or when the same operation has been attempted repeatedly without success or without any state change."
provider: "filesystem"
disable-model-invocation: false
userInvocable: true
---

<skill_instructions>
# Efficient Execution for DeepSeek Harness

## Mission and core principle

**The model decides; the executor batches; verification confirms; the guard
stops loops.**

In long-running harness sessions, cost comes from three places:

1. **Repeated context** - every model turn re-carries the whole conversation,
   including past tool calls and their outputs.
2. **Round-trip inflation** - mechanical steps (copy this file, check that
   file, count those lines) each trigger a full model round trip.
3. **Verbose feedback** - raw tool output floods the context even though the
   model only needs a status, a count, a diff or an exception summary.

This skill keeps the model in the loop only for steps that need judgment,
moves deterministic work into one scripted pass, keeps intermediate
verification local, returns the smallest sufficient evidence, and stops when
progress stalls.

## When to activate this skill

Activate when ANY of these is true:

- A task contains **multiple similar mechanical operations** (copy/move N
  files, search a tree, cleanup a build dir, apply the same edit in many
  places).
- A single tool call would return **large output** (log files, search hits,
  test suites, builds) that the model does not need in full.
- A **modification was just made** and its effect must be confirmed
  ("did the edit land?", "are the copies identical?").
- The **same operation has failed before**, or the same file has been
  modified repeatedly without a verifiable change.
- A **long task** is approaching a delivery point and token/time budget
  matters.

Do NOT activate for:

- Single, one-shot operations where planning a batch would cost more than it
  saves (one grep, one read of a small file).
- Steps that genuinely require model judgment at every point (open-ended
  design, ambiguous requirements, security-sensitive decisions).
- Operations that need interactive or streaming feedback where a batch pass
  would hide required human approval prompts.

## The execution loop (use for every non-trivial task)

```
1. CLASSIFY   - split the task into: deterministic steps (batchable) and
                judgment steps (stay with the model).
2. PLAN       - write the deterministic part as a plan file (JSON) or a
                small script. The plan is the single source of truth.
3. PRE-CHECK  - validate sources, destinations and policies BEFORE writing
                anything (scripts/batch_exec.py does this automatically).
4. EXECUTE    - run the whole deterministic sequence in ONE invocation.
5. VERIFY     - confirm the outcome locally: counts, hashes, diffs.
6. REPORT     - return only the compact receipt + necessary evidence.
7. GUARD      - before any retry, consult scripts/loop_guard.py; honor STOP.
```

Rule: after step 6, if the result is ambiguous, do NOT blindly re-execute.
Go to the "Uncertain results" protocol below.

## Layer playbooks

### Layer 1 - Avoid repeated context cost

- Plan once, in a file. A plan file (JSON) or a generated script can be
  re-read cheaply by YOU; it does not need to be re-sent as conversation
  text. Reference it by path in later turns.
- Keep a running task state in one small file (done / pending / blocked).
  Update it, do not re-narrate it.
- Do not paste large outputs back into the conversation to "remember" them;
  keep them in logs and cite the path.
- When the harness session grows long, prefer delegating a well-scoped
  deterministic subtask to one scripted pass over many turns of narration.

### Layer 2 - Programmatic Tool Calling (PTC): move deterministic work out
### of the model loop

Check first whether the running harness offers a native programmatic tool
calling mechanism:

- **DeepSeek Harness Code Mode / PTC** (if enabled for the session): write
  ONE program (code) that performs the whole deterministic sequence -
  pre-checks, operations, local checks - and returns only the final result
  object. Prefer this when available.
- **Script fallback (always available)**: when no native PTC is enabled,
  achieve the same shape with the harness's existing tools:
  1. write the plan JSON or a small script with the file-write tool;
  2. execute it with the command-execution tool;
  3. read ONLY the summary the script prints.

Both shapes follow the same discipline: **pre-check -> continuous execution
of pre-determined steps -> local intermediate checks -> structured receipt.**

The executor must be able to:

1. Check preconditions before acting (source exists, target writable,
   overwrite policy).
2. Execute several pre-determined operations continuously.
3. Perform intermediate checks and data comparison locally.
4. Record the failing stage, the reason and the affected objects on error.
5. Return only the final summary and the necessary evidence.
6. Pause and return control to the model when judgment is required - it must
   never run unbounded or improvise new actions.

Guard against PTC anti-patterns: if a single native base tool call would do
the job (e.g. `grep -c` for one count), prefer that - do not generate a
program just to use a program. Published experiments show programmatic
calling wins when several tool calls plus filtering are involved, and can be
net-negative for trivial one-liners.

### Layer 3 - Compress tool feedback; keep full detail in logs

Choose the return granularity by task type:

| Task type | Return to the model | Keep in the local log |
|---|---|---|
| Search | total matches, files touched, a few representative results | full result set (JSONL with stable ids) |
| Batch file ops | totals, success/failure counts, verification result | per-operation detail, errors, timings |
| Commands / tests | exit code, pass/fail counts, failure summary | full stdout+stderr |
| Log/page inspection | summary, before/after snippets, aggregated stats | original content |

Rules:

- Never return hundreds of search hit lines; return counts + a small sample.
- For content consistency, compare hashes locally; do not re-read files into
  the conversation just to compare them.
- Never discard the tail of an error log: failures, stack details and key
  evidence must remain retrievable (log path + on-demand reader).
- Deep reads are lazy: read the full log entry only when the summary is
  insufficient to reach a conclusion.
- scripts/search_summary.py and scripts/test_summary.py implement exactly
  this contract (summary to stdout, full JSONL/log to disk).

### Layer 4 - Stop invalid modification, duplicate verification and blind
### retries

- Every important operation gets a **verifiable expected state** before it
  runs (file exists, hash equals X, count equals N).
- After execution, check cheaply first (count, exit code, existence); only
  on discrepancy run the deeper check (hash, diff).
- **Uncertain result protocol** - when it is unclear whether an operation
  succeeded:
  1. Query the current state (do not re-execute).
  2. Decide whether the operation is already done.
  3. Compare current state with the target state.
  4. Only if it is NOT done AND re-execution is safe, decide to retry.
  5. Every retry must have a stated reason (what changed since last time).
  6. On reaching the retry limit, stop and report locatable failure
     evidence.
- **No-progress detection**: same target, same evidence hash, no change
  after an "ok" - that is a no-op, not progress. scripts/loop_guard.py
  flags this as WARN/STOP.
- Never "fix" a failed verification by repeating the modification; the
  verification failure is the signal to change the approach.

## Helper scripts (relative to the skill base directory)

All scripts are stdlib-only Python 3.8+. Run them with the workspace's
python. Logs default to `.dsh-batch-logs/` under the current directory.

### scripts/batch_exec.py - batch executor (Layer 2 + 3 + 4 core)

```
python scripts/batch_exec.py --plan plan.json [--dry-run] [--approve]
                             [--log-dir DIR] [--fail-fast] [--json]
```

Plan JSON:

```json
{
  "plan_id": "copy-configs",
  "description": "human-readable intent",
  "overwrite": "if-different",
  "operations": [
    {"id": "op-1", "op": "copy", "src": "templates/a.conf", "dst": "build/a.conf"},
    {"id": "op-2", "op": "mkdir", "path": "build/out"},
    {"id": "op-3", "op": "expect", "path": "build/a.conf",
     "expect": {"exists": true, "sha256": "abc123..."}},
    {"id": "op-4", "op": "remove", "path": "build/tmp",
     "requires_approval": true}
  ],
  "verify": [
    {"type": "count", "pattern": "build/*.conf", "expect": 8},
    {"type": "tree-hash", "src": "templates", "dst": "build"}
  ]
}
```

- Operations: `copy` (file or tree), `mkdir`, `move`, `remove` (approval
  required), `expect` (assert exists/sha256).
- Overwrite policies: `never` | `if-different` (default) | `always`.
- Idempotent by design: identical content is reported as
  `already_satisfied`, not re-copied.
- Prints a compact receipt (counts + per-op lines + verification) and writes
  the full JSON log. Exit codes: 0 success, 2 partial failure/refusal,
  1 fatal.

### scripts/search_summary.py - batch search with compressed feedback

```
python scripts/search_summary.py --root . --pattern "ERROR|WARN" \
    [--include "*.log"] [--exclude-dir vendor] [--top 5] \
    [--max-line-chars 200] [--json]
python scripts/search_summary.py --show-id m-0041 --log-file <log.jsonl>
```

Returns counts + representative matches; full results in a JSONL log with
stable ids (`m-0001`...). `--show-id` reads one full entry on demand.

### scripts/verify_state.py - post-modification verification

```
python scripts/verify_state.py snapshot --paths a.txt dir/ --out expected.json
python scripts/verify_state.py check --expect expected.json [--json]
python scripts/verify_state.py diff --a old.txt --b new.txt [--log-dir DIR]
```

`snapshot` records hashes; `check` compares against them with PASS/FAIL
evidence; `diff` returns a compact change summary (added/removed/changed
lines, first hunks) with the full diff in a log.

### scripts/test_summary.py - compressed test/command feedback

```
python scripts/test_summary.py -- python -m pytest tests/ -q
python scripts/test_summary.py --show-failure 3 --log-file <run.log>
```

Runs the command, saves the FULL output to a log, prints exit code, counts,
a bounded failure list and the log path. Recognizes pytest / jest / go test
formats; otherwise reports labeled candidate error lines (never invented
counts).

### scripts/loop_guard.py - retry governance (Layer 4)

```
python scripts/loop_guard.py check  --action "fix:src/db.py:line42"
python scripts/loop_guard.py record --action "fix:src/db.py:line42" \
    --result fail --evidence-file src/db.py
python scripts/loop_guard.py status
```

`check` returns PROCEED / WARN / STOP with a reason; `record` logs the
outcome and evidence hash; STOP verdicts must be honored - report the reason
to the user instead of retrying.

## Feedback rules (what to return after each batch)

A good receipt answers, in order:

1. **What was the intent?** (plan id / description)
2. **What happened?** (counts: total / ok / already-satisfied / failed /
   refused)
3. **Was it verified?** (verification section: PASS/FAIL per check with
   evidence)
4. **What is the evidence trail?** (log path for deep reads)
5. **What should happen next?** (only if action is required; otherwise say
   "done")

Never report success on: non-zero exit codes, missing verification PASS,
unread error tails, or STOP verdicts from the loop guard.

## Safety boundaries

- **Approvals are never bypassed.** `remove` operations require BOTH
  `requires_approval: true` in the plan AND `--approve` on the command line.
  Destructive operations (delete, overwrite, move over existing files) are
  refused by default and must be surfaced to the user for explicit
  confirmation through the harness's normal permission flow.
- **The harness's own permission prompts stay in force.** Batching reduces
  model round trips; it does not reduce or skip authorization.
- **Dry-run first for destructive plans**: run with `--dry-run` and show the
  planned effects before any real execution.
- **Path discipline**: operate within the declared workspace; refuse plans
  whose paths escape the workspace root (no `..` traversal above it, no
  absolute paths to system directories).
- **No secrets in logs**: never write credentials, tokens or personal data
  into plan files or logs; scrub before sharing logs.
- **Bounded execution**: every script has explicit exit codes and timeouts
  (`test_summary.py --timeout`); no unbounded loops or infinite retries.
- **Logs are an audit trail**: full logs are written for every batch and
  search; do not delete them mid-task.

## Stop conditions (halt and report, do not improvise)

- The loop guard returns STOP (retry limit, or repeated failure without
  state change).
- Verification fails and the cause is not understood after one focused
  investigation.
- A destructive operation lacks explicit user approval.
- The plan contains operations outside the safety boundaries above.
- The task's success criteria cannot be expressed as verifiable expectations.

When stopping, report: what was attempted, the locatable evidence (log
paths, hashes, exit codes), the reason for stopping, and the options the
user can choose from.

## Output format conventions

- Receipts use the `[receipt]` / `counts:` / `verify:` / `full log:` lines
  emitted by the scripts - keep them verbatim in your reply so the evidence
  is traceable.
- Cite log paths exactly; they are the on-demand deep-read entry points.
- Distinguish measured facts from inference: mark anything not directly
  observed as unverified.

## 中文摘要

本 Skill 面向 DeepSeek Harness 的长任务效率问题：模型只参与需要判断的步骤，
可确定的机械操作交给脚本批量执行（PTC 思路），中间核验留在本地，只向模型
返回最少但充分的证据（计数、状态、哈希、差异摘要），并在任务缺乏进展时及时
停止无效循环。五个辅助脚本：`batch_exec.py`（计划驱动的批量执行+本地核验）、
`search_summary.py`（批量搜索+摘要+按需深读）、`verify_state.py`（修改后状态
核验：快照/检查/差异）、`test_summary.py`（命令与测试输出压缩+完整日志）、
`loop_guard.py`（重试治理与无进展检测）。删除/覆盖等高风险操作必须显式批准，
绝不绕过 Harness 自身的权限确认。
</skill_instructions>

<skill_resources>
Base directory for this skill: <the directory that contains this SKILL.md>
Resolve relative paths mentioned by this skill against the base directory
before using them.
- scripts/batch_exec.py     - batch executor with plan JSON + verification
- scripts/search_summary.py - batch search, compressed summary, JSONL log
- scripts/verify_state.py   - snapshot / check / diff with hash evidence
- scripts/test_summary.py   - compressed command & test feedback, full log
- scripts/loop_guard.py     - retry governance and no-progress detection
- references/ptc-playbook.md - deeper guidance: when to batch, feedback
                               granularity table, worked examples
- examples/sample-plan.json  - a complete worked plan example
</skill_resources>
