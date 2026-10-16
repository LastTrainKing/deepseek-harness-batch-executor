# PTC & Batch-Execution Playbook

Deep guidance for the `deepseek-harness-batch-executor` skill. Read this when
the quick rules in `SKILL.md` are not enough - for example when deciding
whether a task should be batched at all, or how much detail a tool response
should carry.

## 1. Why batch at all? The three cost centers

In a long agent session the bill has three parts:

| Cost center | What it is | Who pays it |
|---|---|---|
| Model input tokens | Every request re-carries the conversation: past user messages, past assistant messages, past tool calls and their results | grows with every turn |
| Model output tokens | Generated text + tool-call arguments | grows when the model "talks to itself" through many small steps |
| Tool execution + harness overhead | Process launches, file IO, message serialization, state management | grows with the number of tool calls |

A naive loop - "copy file, read result, copy next file, read result, ..." -
pays all three costs N times. A batch pass pays the model cost once (the
plan) and the execution cost once (the program), and returns one receipt.

Important caveat: this skill never *assumes* how a specific harness
serializes history. Whether your harness re-sends the full transcript, a
compacted summary, or something else must be verified on the target system
(session logs, token counters). The batching discipline below is beneficial
under any of those models, but the *magnitude* of savings differs.

## 2. Decision guide: batch, single call, or native PTC?

```
Is the step deterministic (no judgment needed)?
├── No  -> keep it with the model (one focused tool call or one question)
└── Yes -> Is there more than one similar step, or is the output large?
    ├── No  -> use ONE native tool call (grep -c, cat small file, single cp)
    │         A generated program would be net-negative here.
    └── Yes -> Does the harness offer native programmatic tool calling
              │         (DSH Code Mode / PTC)?
              │   ├── Yes -> write ONE program: pre-check -> run sequence
              │   │          -> local checks -> return the result object.
              │   └── No  -> script fallback:
              │              1. write plan JSON / small script (file tool)
              │              2. execute it (command tool)
              │              3. read only the printed summary
              └── Is any single step destructive or approval-gated?
                  └── Yes -> keep that step OUTSIDE the batch; surface it
                            for explicit user approval.
```

Anti-patterns to avoid:

- Batching a single trivial operation (one `cp`) - planning costs more than
  it saves.
- Stuffing judgment calls into a script ("if tests fail then decide whether
  to refactor") - scripts execute, they do not decide.
- Unbounded scripts: every batch has an explicit operation list and exit
  codes.
- Hiding approval prompts: a batch that deletes 50 files without showing the
  user the plan is a safety bug, not an optimization.

## 3. Feedback granularity table

Choose the smallest response that still answers the model's actual question.

| Situation | Return | Never return |
|---|---|---|
| Search over a tree | total matches, files touched, per-file counts, ≤5 representative hits (spread across files) | all matching lines, full file contents |
| Batch copy/move | total, ok, already-satisfied, failed, refused + verification PASS/FAIL | per-file success chatter, file contents |
| Test/build run | exit code, pass/fail counts, bounded failure list with file:line | the full stdout/stderr (log path instead) |
| After an edit | hash/diff evidence: what changed (line counts, first hunks) | the whole before/after file |
| Log triage | counts by severity, a few samples, time range | thousands of raw lines |
| Page/document fetch | extracted summary or relevant section | the entire HTML/text dump |

Deep-read contract: every summary must carry a **locator** - a log path plus
a stable entry id - so a follow-up question costs one targeted read, not a
re-run.

## 4. Worked example: batch copy with verification

Task: "Copy the 12 config templates into build/configs and make sure every
file arrived intact."

Naive pattern (12 files): 12 copy calls + 12 verification reads ≈ 24 model
round trips, each re-carrying the transcript.

Batched pattern:

1. Write `plan.json` (12 copy ops + 1 count verify + 1 tree-hash verify).
2. Run `python scripts/batch_exec.py --plan plan.json`.
3. Read the receipt:

```
[receipt] plan=copy-configs status=success elapsed_ms=41
counts: total=12 ok=12 already_satisfied=0 failed=0 refused=0
verify:
  count      pass  glob 'build/configs/*.conf' matched 12 file(s), expected 12
  tree-hash  pass  trees identical (12 file(s))
full log: .dsh-batch-logs/copy-configs-20261017T043700Z.json
```

Two model round trips total, and the verification is *stronger* than the
naive pattern (full content hashes, not "the command returned 0").

## 5. Worked example: log triage without flooding the context

Task: "Find all ERROR lines in logs/ and summarize."

```
python scripts/search_summary.py --root logs --pattern "ERROR" --top 5
```

Returns: `total_matches=147 files_touched=12`, per-file counts, 5
representative hits, and the full JSONL log. If the model then needs the
exact text of hit #41:

```
python scripts/search_summary.py --show-id m-0041 --log-file <log.jsonl>
```

One targeted read instead of re-running the search or reading whole files.

## 6. Worked example: retry governance

Task: "Fix the failing assertion in tests/test_db.py".

```
python scripts/loop_guard.py check --action "fix:tests/test_db.py:assert42"
# -> PROCEED (first attempt)
... apply fix, run tests via test_summary.py ...
python scripts/loop_guard.py record --action "fix:tests/test_db.py:assert42" \
    --result fail --evidence-file tests/test_db.py
... check again before any second attempt ...
```

If the fix did not change the file (same evidence hash) or the same failure
recurs past the retry limit, the guard returns STOP with a reason - the
model stops, reports the locatable evidence, and asks the user for direction
instead of burning turns on blind retries.

## 7. Measuring the effect honestly

What this skill can measure in any environment:

- model tool calls and model round trips (count them)
- wall-clock execution time of the deterministic part
- size of the returned payload (characters/bytes) vs. size of the full log
- success / partial / failure counts and verification results

What it CANNOT measure without harness-side statistics:

- model input/output tokens per request (requires the harness or API to
  expose per-request usage)

Recommended measurement protocol on DeepSeek Harness:

1. Run the same task twice: naive (one tool call per mechanical step) and
   batched (this skill).
2. Export the session logs (DSH keeps append-only session logs; recent RC
   versions expose per-turn token and timing statistics).
3. Sum input tokens, output tokens, tool-call counts and elapsed time per
   run; report both runs side by side with identical initial data and model
   configuration.

Reference points from public sources (NOT measured by this project - cite
them as external data only):

- A published DSH PTC experiment (log statistics task): 66,496 vs 55,043
  total tokens (-17%), tool calls 4 -> 1. Source: Tencent Cloud developer
  community article "拆解DeepSeek Harness(四):PTC模式" (2026-08).
- Anthropic's "Code execution with MCP" engineering blog (2025-11): token
  consumption 150k -> 2k (-98.7%) in their example, via code-orchestrated
  tool calls with in-sandbox filtering.

Token savings are task-dependent: they are large when many tool results
would otherwise flow through the model, and near zero (or negative) when a
single native tool call suffices. Do not quote a fixed percentage as this
project's result.

## 8. Compatibility notes

- Scripts: Python 3.8+, standard library only - no pip install needed.
- Platforms: developed and tested on Windows (Git Bash); uses `os.path`
  semantics throughout, so Linux/macOS behave the same. `chmod`-based
  permission tests are not portable; test refusals via the approval flag
  and real filesystem errors instead.
- The skill itself (SKILL.md + scripts) is harness-agnostic: the format
  follows DeepSeek Harness's skill specification (frontmatter + skill
  instructions/resources sections), and the same scripts work in any
  harness that can execute Python.
