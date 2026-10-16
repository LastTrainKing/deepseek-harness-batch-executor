#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""search_summary.py - Batch search with compressed feedback.

Part of the ``deepseek-harness-batch-executor`` skill for DeepSeek Harness.

Searches a file tree for a regex (or literal) pattern and returns only a
summary: total match count, number of files touched, per-file counts and a
few representative matches (spread across files). The COMPLETE result set is
written to a JSONL log, so the model can read specific entries on demand
instead of receiving hundreds of lines in the conversation.

On-demand deep reads:
  --show-id m-0041    print one complete log entry (full, untruncated line)
  --read-log          print the full log path only (for scripting)

Exit codes: 0 = ran (matches or not), 1 = fatal error (bad root/pattern).

Stdlib only. Python 3.8+.
"""

import argparse
import datetime
import fnmatch
import json
import os
import re
import sys
import time

DEFAULT_EXCLUDES = (".git", "node_modules", "__pycache__", ".dsh-batch-logs",
                    ".venv", "venv", "dist", "build", ".idea")


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def looks_binary(path):
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(8192)
        return b"\x00" in chunk
    except OSError:
        return True


def search_tree(root, rx, includes, excludes):
    """Return (matches, per_file_counts). matches keep the FULL line text."""
    matches = []
    per_file = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in excludes
            and not os.path.islink(os.path.join(dirpath, d)))
        for fn in sorted(filenames):
            if includes and not any(fnmatch.fnmatch(fn, g) for g in includes):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if looks_binary(full):
                continue
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    for n, line in enumerate(fh, 1):
                        if rx.search(line):
                            matches.append({"file": rel, "line": n,
                                            "text": line.rstrip("\r\n")})
                            per_file[rel] = per_file.get(rel, 0) + 1
            except OSError:
                continue
    return matches, per_file


def pick_representative(matches, top_n):
    """Round-robin across files so the sample is not dominated by one file."""
    by_file = {}
    for m in matches:
        by_file.setdefault(m["file"], []).append(m)
    files = sorted(by_file)
    picked = []
    idx = 0
    while len(picked) < top_n and idx < max((len(v) for v in by_file.values()),
                                             default=0):
        for f in files:
            if len(picked) >= top_n:
                break
            if idx < len(by_file[f]):
                picked.append(by_file[f][idx])
        idx += 1
    return picked


def show_id(log_path, entry_id):
    with open(log_path, "r", encoding="utf-8") as fh:
        for line in fh:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("id") == entry_id:
                print(json.dumps(entry, indent=2, ensure_ascii=False))
                return 0
    print("error: entry %r not found in %s" % (entry_id, log_path),
          file=sys.stderr)
    return 1


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Search a file tree and return a compressed summary; "
                    "full results go to a JSONL log for on-demand reading.")
    ap.add_argument("--root", default=".", help="directory to search (default: .)")
    ap.add_argument("--pattern",
                    help="regex pattern (not required with --show-id)")
    ap.add_argument("--literal", action="store_true",
                    help="treat --pattern as a literal substring, not a regex")
    ap.add_argument("--include", action="append", default=[],
                    help="filename glob to include, e.g. '*.log' (repeatable)")
    ap.add_argument("--exclude-dir", action="append", default=[],
                    help="directory name to prune, in addition to the built-in "
                         "defaults (.git, node_modules, ...) (repeatable)")
    ap.add_argument("--top", type=int, default=5,
                    help="number of representative matches in the summary "
                         "(default: 5)")
    ap.add_argument("--max-line-chars", type=int, default=200,
                    help="trim summary lines to this many characters; full text "
                         "stays in the log (default: 200)")
    ap.add_argument("--log-dir", default=".dsh-batch-logs",
                    help="directory for the full JSONL log "
                         "(default: .dsh-batch-logs)")
    ap.add_argument("--show-id", metavar="ID",
                    help="print one complete entry from the log and exit")
    ap.add_argument("--log-file",
                    help="explicit log path for --show-id")
    ap.add_argument("--json", action="store_true",
                    help="print the summary as JSON instead of text")
    args = ap.parse_args(argv)

    if args.show_id:
        log_path = args.log_file
        if not log_path:
            print("error: --show-id requires --log-file", file=sys.stderr)
            return 1
        if not os.path.isfile(log_path):
            print("error: log not found: %s" % log_path, file=sys.stderr)
            return 1
        return show_id(log_path, args.show_id)

    if not args.pattern:
        ap.error("--pattern is required unless using --show-id")

    if not os.path.isdir(args.root):
        print("error: root not found: %s" % args.root, file=sys.stderr)
        return 1
    try:
        rx = re.compile(re.escape(args.pattern) if args.literal
                        else args.pattern)
    except re.error as exc:
        print("error: invalid pattern: %s" % exc, file=sys.stderr)
        return 1

    excludes = set(DEFAULT_EXCLUDES) | set(args.exclude_dir)
    started = time.time()
    matches, per_file = search_tree(args.root, rx, args.include, excludes)
    elapsed_ms = int((time.time() - started) * 1000)

    # write the full log with stable ids
    os.makedirs(args.log_dir, exist_ok=True)
    log_path = os.path.join(
        args.log_dir,
        "search-%s.jsonl" % time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    for i, m in enumerate(matches, 1):
        m["id"] = "m-%04d" % i
    with open(log_path, "w", encoding="utf-8") as fh:
        for m in matches:
            fh.write(json.dumps(m, ensure_ascii=False) + "\n")

    # compact summary
    def trim(text):
        if len(text) > args.max_line_chars:
            return text[:args.max_line_chars] + "...[truncated, full text in log]"
        return text

    rep = pick_representative(matches, args.top)
    summary = {
        "root": args.root,
        "pattern": args.pattern,
        "literal": bool(args.literal),
        "total_matches": len(matches),
        "files_touched": len(per_file),
        "per_file_counts": dict(sorted(per_file.items(),
                                       key=lambda kv: (-kv[1], kv[0]))),
        "representative": [
            {"id": r["id"], "file": r["file"], "line": r["line"],
             "text": trim(r["text"])}
            for r in rep
        ],
        "log_path": log_path,
        "elapsed_ms": elapsed_ms,
    }

    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print("[search] root=%s pattern=%s (%s)"
              % (args.root, args.pattern,
                 "literal" if args.literal else "regex"))
        print("total_matches=%d files_touched=%d elapsed_ms=%d"
              % (len(matches), len(per_file), elapsed_ms))
        if rep:
            print("top %d representative (spread across files):" % len(rep))
            for r in summary["representative"]:
                print("  %s %s:%d  %s" % (r["id"], r["file"], r["line"],
                                          r["text"]))
        else:
            print("no matches found")
        print("per-file counts: %s"
              % ", ".join("%s=%d" % kv for kv in
                          list(summary["per_file_counts"].items())[:10]))
        print("full results (%d entries): %s" % (len(matches), log_path))
        print("on-demand deep read: python scripts/search_summary.py "
              "--show-id %s --log-file %s"
              % (rep[0]["id"] if rep else "m-0001", log_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
