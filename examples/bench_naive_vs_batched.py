#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bench_naive_vs_batched.py - Reproducible comparison: naive step-by-step
execution vs. batched execution with this skill's tooling.

What is measured (both patterns run the SAME task on the SAME fixtures):

- wall-clock time of the deterministic part (seconds)
- number of command executions (each one is one model tool call in the
  naive pattern; the batched pattern uses ONE execution)
- payload size returned to the caller (characters): naive = sum of the
  per-step outputs the model would have received; batched = size of the
  single receipt

What is NOT measured here (and must not be fabricated):

- model input/output tokens. Token counts depend on how the harness
  serializes history into each request and cannot be derived from character
  counts. Measure them on the target harness (see
  references/ptc-playbook.md section 7) - e.g. DeepSeek Harness session
  logs / per-turn statistics.

Usage:
    python examples/bench_naive_vs_batched.py [--files 100] [--iterations 3]
                                              [--keep-fixtures]
Results are printed as a table and written to bench-results.json.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
BATCH_EXEC = os.path.join(REPO, "scripts", "batch_exec.py")


def make_fixtures(root, n):
    src = os.path.join(root, "fixtures")
    os.makedirs(src, exist_ok=True)
    for i in range(n):
        with open(os.path.join(src, "file-%04d.txt" % i), "w",
                  encoding="utf-8") as fh:
            fh.write("fixture file %04d\n" % i)
            fh.write("payload line a\npayload line b\n" * 20)
    return src


def build_plan(src, dst, n):
    ops = [{"id": "op-%d" % (i + 1), "op": "copy",
            "src": os.path.join(src, "file-%04d.txt" % i),
            "dst": os.path.join(dst, "file-%04d.txt" % i)}
           for i in range(n)]
    return {
        "plan_id": "bench-copy",
        "description": "benchmark: copy %d files with verification" % n,
        "overwrite": "if-different",
        "operations": ops,
        "verify": [
            {"type": "count", "pattern": os.path.join(dst, "*.txt"),
             "expect": n},
            {"type": "tree-hash", "src": src, "dst": dst},
        ],
    }


def run_naive(python, src, dst, n):
    """Naive pattern: one command per copy step (one model tool call each).

    Returns (seconds, command_count, payload_chars_returned).
    """
    os.makedirs(dst, exist_ok=True)
    started = time.time()
    payload = 0
    cmds = 0
    for i in range(n):
        s = os.path.join(src, "file-%04d.txt" % i)
        d = os.path.join(dst, "file-%04d.txt" % i)
        # step 1: copy (a real command, e.g. `cp`)
        proc = subprocess.run([python, "-c",
                               "import shutil,sys;shutil.copy2(sys.argv[1],sys.argv[2])",
                               s, d], capture_output=True, text=True)
        cmds += 1
        payload += len(proc.stdout) + len(proc.stderr)
        # step 2: verify the copy landed (a real second command, e.g. `cmp`)
        proc = subprocess.run([python, "-c",
                               "import sys;sys.exit(0 if open(sys.argv[1],'rb').read()==open(sys.argv[2],'rb').read() else 1)",
                               s, d], capture_output=True, text=True)
        cmds += 1
        payload += len(proc.stdout) + len(proc.stderr)
    return time.time() - started, cmds, payload


def run_batched(python, plan_path, log_dir):
    """Batched pattern: ONE command does pre-check + copy + verify."""
    started = time.time()
    proc = subprocess.run(
        [python, BATCH_EXEC, "--plan", plan_path, "--log-dir", log_dir],
        capture_output=True, text=True)
    elapsed = time.time() - started
    return elapsed, 1, len(proc.stdout) + len(proc.stderr), proc.returncode


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--files", type=int, default=100)
    ap.add_argument("--iterations", type=int, default=3)
    ap.add_argument("--keep-fixtures", action="store_true")
    args = ap.parse_args(argv)

    python = sys.executable
    root = tempfile.mkdtemp(prefix="dsh-bench-")
    try:
        src = make_fixtures(root, args.files)
        plan_path = os.path.join(root, "plan.json")
        results = {"files": args.files, "iterations": args.iterations,
                   "python": sys.version.split()[0], "platform": sys.platform,
                   "naive": [], "batched": [], "notes": []}

        for it in range(args.iterations):
            dst_naive = os.path.join(root, "out-naive-%d" % it)
            secs, cmds, payload = run_naive(python, src, dst_naive,
                                            args.files)
            results["naive"].append({"iteration": it, "seconds": round(secs, 4),
                                     "commands": cmds,
                                     "payload_chars": payload})

            dst_batch = os.path.join(root, "out-batch-%d" % it)
            plan = build_plan(src, dst_batch, args.files)
            with open(plan_path, "w", encoding="utf-8") as fh:
                json.dump(plan, fh, indent=2)
            log_dir = os.path.join(root, "logs-%d" % it)
            secs, cmds, payload, rc = run_batched(python, plan_path, log_dir)
            results["batched"].append({"iteration": it, "seconds": round(secs, 4),
                                       "commands": cmds,
                                       "payload_chars": payload,
                                       "exit_code": rc})
            if rc != 0:
                results["notes"].append(
                    "iteration %d: batched run exited %d" % (it, rc))

        def avg(rows, key):
            return round(sum(r[key] for r in rows) / len(rows), 4)

        summary = {
            "naive_avg_seconds": avg(results["naive"], "seconds"),
            "batched_avg_seconds": avg(results["batched"], "seconds"),
            "naive_avg_commands": avg(results["naive"], "commands"),
            "batched_avg_commands": avg(results["batched"], "commands"),
            "naive_avg_payload_chars": avg(results["naive"], "payload_chars"),
            "batched_avg_payload_chars": avg(results["batched"],
                                             "payload_chars"),
        }
        summary["command_reduction_factor"] = round(
            summary["naive_avg_commands"] / summary["batched_avg_commands"], 1)
        summary["time_ratio_batched_over_naive"] = round(
            summary["batched_avg_seconds"] / summary["naive_avg_seconds"], 3)
        results["summary"] = summary
        results["measurement_notes"] = [
            "commands = number of process executions; in the naive pattern "
            "each execution corresponds to one model tool call (copy + "
            "verify per file).",
            "payload_chars = size of output the caller would receive; this "
            "is NOT a token count and must not be reported as one.",
            "model input/output tokens are NOT measured here; see "
            "references/ptc-playbook.md section 7 for the protocol.",
        ]

        out_path = os.path.join(os.getcwd(), "bench-results.json")
        try:
            with open(out_path, "w", encoding="utf-8") as fh:
                json.dump(results, fh, indent=2, ensure_ascii=False)
        except OSError:
            out_path = "(could not write bench-results.json in cwd)"

        print("benchmark: %d files, %d iterations (python %s on %s)"
              % (args.files, args.iterations, results["python"],
                 results["platform"]))
        print("%-28s %12s %12s" % ("metric", "naive", "batched"))
        print("%-28s %12.4f %12.4f" % ("avg seconds",
                                       summary["naive_avg_seconds"],
                                       summary["batched_avg_seconds"]))
        print("%-28s %12.1f %12.1f" % ("avg command executions",
                                       summary["naive_avg_commands"],
                                       summary["batched_avg_commands"]))
        print("%-28s %12.1f %12.1f" % ("avg payload chars returned",
                                       summary["naive_avg_payload_chars"],
                                       summary["batched_avg_payload_chars"]))
        print("command executions reduced by factor: %s"
              % summary["command_reduction_factor"])
        print("NOT measured: model input/output tokens (harness-dependent; "
              "see references/ptc-playbook.md section 7)")
        print("results written to: %s" % out_path)
        return 0
    finally:
        if args.keep_fixtures:
            print("fixtures kept at: %s" % root)
        else:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
