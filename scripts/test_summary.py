#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_summary.py - Run a command (tests, builds, linters) with compressed
feedback.

Part of the ``deepseek-harness-batch-executor`` skill for DeepSeek Harness.

Runs a command, captures ALL output (stdout+stderr) to a local log file, and
prints only a summary: exit code, pass/fail counts (recognized formats:
pytest, jest, go test; otherwise a generic candidate-error scan), a bounded
failure list, and the log path.

On-demand deep reads:
  --show-failure N     print the N-th failure's full text from the log
                       (no re-run of the command)
  --tail N             print the last N log lines

The full log is never discarded: tail errors, failure details and key
evidence stay retrievable even though they are not part of the summary.

Exit codes: 0 = command succeeded, 2 = command failed (summary still printed),
3 = fatal error (command could not be run), 4 = timeout.

Stdlib only. Python 3.8+.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time

MAX_FAILURES_DEFAULT = 10
MAX_FAILURE_CHARS_DEFAULT = 300


def parse_output(text):
    """Extract counts and failures from test-runner output.

    Recognized formats: pytest, jest, go test. Everything else falls back to
    a generic 'candidate error line' scan (never claims counts it cannot
    find)."""
    info = {"format": "generic", "counts": {}, "failures": []}

    m = re.search(r"(\d+)\s+failed", text)
    if m:
        info["counts"]["failed"] = int(m.group(1))
    m = re.search(r"(\d+)\s+passed", text)
    if m:
        info["counts"]["passed"] = int(m.group(1))
    m = re.search(r"(\d+)\s+error", text)
    if m:
        info["counts"]["error"] = int(m.group(1))

    if re.search(r"pytest|FAILED\s+\S+::|short test summary", text):
        info["format"] = "pytest"
    elif re.search(r"Tests:\s+.*\d+ failed|● ", text):
        info["format"] = "jest"
    elif re.search(r"--- FAIL:|^FAIL\s+\S+", text, re.M):
        info["format"] = "go"
    elif info["counts"]:
        info["format"] = ("generic (pass/fail counts detected; framework "
                          "not recognized)")

    if info["format"] == "pytest":
        info["failures"] = [
            "%s :: %s" % (f, d) if d else f
            for f, d in re.findall(r"^FAILED\s+(\S+)(?:\s+-\s+(.*))?$",
                                   text, re.M)]
    elif info["format"] == "jest":
        info["failures"] = re.findall(r"^●\s+(.*)$", text, re.M)
    elif info["format"] == "go":
        info["failures"] = re.findall(r"^--- FAIL:\s+(\S+)", text, re.M)

    if not info["failures"] and not info["counts"]:
        # generic scan: keep bounded, clearly labeled candidates
        cands = []
        for line in text.splitlines():
            s = line.strip()
            if not s:
                continue
            if re.search(r"error|failed|failure|traceback|exception|assert",
                         s, re.I):
                if s not in cands:
                    cands.append(s)
        info["failures"] = cands[:MAX_FAILURES_DEFAULT]
        info["format"] = "generic (candidate error lines, not a framework summary)"
    return info


def show_failure(log_path, index):
    with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    info = parse_output(text)
    failures = info.get("failures", [])
    if index < 1 or index > len(failures):
        print("error: failure index %d out of range (1..%d)"
              % (index, len(failures)), file=sys.stderr)
        return 1
    target = failures[index - 1]
    print("failure #%d: %s" % (index, target))
    # print surrounding context from the log: the block that contains the
    # failure identifier, plus +/- 10 lines
    lines = text.splitlines()
    hit = None
    key = target.split(" :: ")[0].split(" (")[0]
    for i, line in enumerate(lines):
        if key and key in line:
            hit = i
            break
    if hit is None:
        print("(failure identifier not located verbatim in the log; "
              "showing the last 30 lines instead)")
        block = lines[-30:]
    else:
        block = lines[max(0, hit - 10):hit + 20]
    print("--- context from log %s ---" % log_path)
    print("\n".join(block))
    return 0


def tail(log_path, n):
    with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()
    print("\n".join(lines[-n:]))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Run a command and return a compressed summary; full "
                    "output goes to a local log for on-demand deep reads.")
    ap.add_argument("--log-dir", default=".dsh-batch-logs")
    ap.add_argument("--timeout", type=float, default=300.0,
                    help="seconds before the command is killed (default: 300)")
    ap.add_argument("--max-failures", type=int, default=MAX_FAILURES_DEFAULT)
    ap.add_argument("--max-failure-chars", type=int,
                    default=MAX_FAILURE_CHARS_DEFAULT)
    ap.add_argument("--show-failure", type=int, metavar="N",
                    help="print failure N with context from the log and exit")
    ap.add_argument("--log-file", help="explicit log path for --show-failure")
    ap.add_argument("--tail", type=int, metavar="N",
                    help="print the last N lines of the log and exit")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("command", nargs=argparse.REMAINDER,
                    help="command to run, after '--'")
    args = ap.parse_args(argv)

    if args.show_failure or args.tail:
        log_path = args.log_file
        if not log_path or not os.path.isfile(log_path):
            print("error: --show-failure/--tail require an existing --log-file",
                  file=sys.stderr)
            return 1
        if args.show_failure:
            return show_failure(log_path, args.show_failure)
        return tail(log_path, args.tail)

    cmd = args.command
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("fatal: no command given (use: test_summary.py -- <cmd...>)",
              file=sys.stderr)
        return 3

    started = time.time()
    timed_out = False
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=args.timeout, universal_newlines=True)
        output = proc.stdout or ""
        exit_code = proc.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        output = (exc.output or "")
        if isinstance(output, bytes):
            output = output.decode("utf-8", "replace")
        exit_code = None
    except OSError as exc:
        print("fatal: cannot run %s: %s" % (" ".join(cmd), exc),
              file=sys.stderr)
        return 3
    elapsed_ms = int((time.time() - started) * 1000)

    # full log - never discarded
    os.makedirs(args.log_dir, exist_ok=True)
    log_path = os.path.join(
        args.log_dir,
        "run-%s.log" % time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write("$ %s\nexit=%s elapsed_ms=%d\n--- output ---\n%s\n"
                 % (" ".join(cmd), exit_code, elapsed_ms, output))

    info = parse_output(output)
    failures = info["failures"][:args.max_failures]
    truncated = info["failures"][args.max_failures:]

    summary = {
        "command": cmd,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "elapsed_ms": elapsed_ms,
        "format": info["format"],
        "counts": info["counts"],
        "failures": [f[:args.max_failure_chars] for f in failures],
        "failures_truncated": len(truncated),
        "output_chars": len(output),
        "log_path": log_path,
    }

    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print("[run] %s" % " ".join(cmd))
        if timed_out:
            print("status: TIMEOUT after %ds (killed)" % args.timeout)
        else:
            print("status: exit=%d elapsed_ms=%d" % (exit_code, elapsed_ms))
        if info["counts"]:
            print("counts: %s" % ", ".join(
                "%s=%d" % kv for kv in sorted(info["counts"].items())))
        print("format: %s" % info["format"])
        if failures:
            print("failures (%d shown%s):"
                  % (len(failures),
                     ", %d more in log" % len(truncated) if truncated else ""))
            for i, f in enumerate(failures, 1):
                print("  %d. %s" % (i, f[:args.max_failure_chars]))
        elif exit_code == 0 and not timed_out:
            print("failures: none (command succeeded)")
        print("full output (%d chars): %s" % (len(output), log_path))
        print("on-demand: --show-failure N --log-file %s   or   --tail N "
              "--log-file %s" % (log_path, log_path))

    if timed_out:
        return 4
    return 0 if exit_code == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
