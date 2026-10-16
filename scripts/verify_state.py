#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_state.py - Verify file state after modifications, with evidence.

Part of the ``deepseek-harness-batch-executor`` skill for DeepSeek Harness.

Answers "did the change actually land?" with hashes and diffs instead of
another full-file read:

  snapshot  Record current state (exists / size / sha256) of paths into an
            expectations JSON file.
  check     Compare current state against an expectations file. Prints
            PASS/FAIL per entry with concrete evidence (expected vs actual
            hash prefix), so a failed edit is visible as data.
  diff      Compare two paths (files or directory trees). Prints a compact
            change summary (added/removed/changed files, line-level stats,
            first hunks trimmed) and writes the FULL diff to a log file.

Exit codes: snapshot 0 / 1; check 0 = all pass, 2 = any fail, 1 = fatal;
diff 0 = computed (result in output), 1 = fatal.

Stdlib only. Python 3.8+.
"""

import argparse
import datetime
import difflib
import hashlib
import json
import os
import sys
import time


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def path_hashes(path):
    """Return {relpath: sha256} for a file or every file under a directory."""
    if os.path.isfile(path):
        return {os.path.basename(path): sha256_file(path)}
    out = {}
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames.sort()
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, path).replace(os.sep, "/")
            out[rel] = sha256_file(full)
    return out


def snapshot(paths):
    entries = []
    for p in paths:
        entry = {"path": p, "exists": os.path.exists(p)}
        if os.path.isfile(p):
            entry["sha256"] = sha256_file(p)
            entry["size"] = os.path.getsize(p)
        elif os.path.isdir(p):
            entry["tree"] = path_hashes(p)
            entry["files"] = len(entry["tree"])
        entries.append(entry)
    return {"created_at": utc_now(), "entries": entries}


def check(expect_path):
    with open(expect_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    results = []
    for e in data.get("entries", []):
        path = e.get("path")
        rec = {"path": path}
        if not os.path.exists(path):
            if e.get("exists"):
                rec["status"] = "FAIL"
                rec["detail"] = "expected to exist, but it does not"
            else:
                rec["status"] = "PASS"
                rec["detail"] = "confirmed absent"
            results.append(rec)
            continue
        if e.get("exists") is False:
            rec["status"] = "FAIL"
            rec["detail"] = "expected absent, but it exists"
            results.append(rec)
            continue
        if os.path.isfile(path) and "sha256" in e:
            actual = sha256_file(path)
            if actual == e["sha256"]:
                rec["status"] = "PASS"
                rec["detail"] = "sha256 matches (%s...)" % actual[:12]
            else:
                rec["status"] = "FAIL"
                rec["detail"] = ("sha256 mismatch: expected %s..., actual %s..."
                                 % (e["sha256"][:12], actual[:12]))
        elif os.path.isdir(path) and "tree" in e:
            actual = path_hashes(path)
            if actual == e["tree"]:
                rec["status"] = "PASS"
                rec["detail"] = "tree identical (%d file(s))" % len(actual)
            else:
                only_exp = sorted(set(e["tree"]) - set(actual))
                only_act = sorted(set(actual) - set(e["tree"]))
                diff = sorted(k for k in set(e["tree"]) & set(actual)
                              if e["tree"][k] != actual[k])
                rec["status"] = "FAIL"
                rec["detail"] = ("tree differs: missing=%s extra=%s changed=%s"
                                 % (only_exp[:5], only_act[:5], diff[:5]))
        else:
            rec["status"] = "PASS"
            rec["detail"] = "exists (no hash expectation declared)"
        results.append(rec)
    return results


def diff_paths(a, b, max_hunks, max_hunk_chars, log_dir):
    """Compare two files or trees. Returns (report_dict, human_lines, log_path)."""
    report = {"a": a, "b": b}
    lines = []

    if os.path.isfile(a) and os.path.isfile(b):
        with open(a, "r", encoding="utf-8", errors="replace") as fh:
            ta = fh.read().splitlines()
        with open(b, "r", encoding="utf-8", errors="replace") as fh:
            tb = fh.read().splitlines()
        dl = list(difflib.unified_diff(ta, tb, fromfile=a, tofile=b, n=3))
        added = sum(1 for l in dl if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in dl if l.startswith("-") and not l.startswith("---"))
        report["result"] = "identical" if not dl else "differ"
        report["added_lines"] = added
        report["removed_lines"] = removed
        lines.append("file diff: %s vs %s -> %s (+%d / -%d line(s))"
                     % (a, b, report["result"], added, removed))
        if dl:
            shown = dl[:max_hunks]
            lines.append("--- first %d diff line(s) (full diff in log) ---"
                         % len(shown))
            lines.append("\n".join(l[:max_hunk_chars] for l in shown))
    elif os.path.isdir(a) and os.path.isdir(b):
        ha, hb = path_hashes(a), path_hashes(b)
        only_a = sorted(set(ha) - set(hb))
        only_b = sorted(set(hb) - set(ha))
        changed = sorted(k for k in set(ha) & set(hb) if ha[k] != hb[k])
        report["result"] = "identical" if not (only_a or only_b or changed) \
            else "differ"
        report["only_in_a"] = only_a
        report["only_in_b"] = only_b
        report["changed"] = changed
        lines.append("tree diff: %s vs %s -> %s" % (a, b, report["result"]))
        lines.append("  only in A (%d): %s" % (len(only_a), only_a[:10]))
        lines.append("  only in B (%d): %s" % (len(only_b), only_b[:10]))
        lines.append("  changed content (%d): %s" % (len(changed), changed[:10]))
        for k in changed[:max_hunks]:
            try:
                with open(os.path.join(a, k), "r", encoding="utf-8",
                          errors="replace") as fh:
                    ta = fh.read().splitlines()
                with open(os.path.join(b, k), "r", encoding="utf-8",
                          errors="replace") as fh:
                    tb = fh.read().splitlines()
                dl = list(difflib.unified_diff(
                    ta, tb, fromfile=os.path.join(a, k),
                    tofile=os.path.join(b, k), n=2))
                lines.append("--- diff %s (first %d line(s)) ---"
                             % (k, max_hunk_chars))
                lines.append("\n".join(dl[:max_hunk_chars]))
            except OSError as exc:
                lines.append("  (cannot diff %s: %s)" % (k, exc))
    else:
        report["result"] = "error"
        lines.append("error: paths are not comparable "
                     "(need two files or two directories)")
        return report, lines, None

    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(
        log_dir, "diff-%s.txt" % time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    report["log_path"] = log_path
    return report, lines, log_path


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Snapshot / check / diff file state with hash evidence.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_snap = sub.add_parser("snapshot", help="record current state")
    p_snap.add_argument("--paths", nargs="+", required=True)
    p_snap.add_argument("--out", required=True, help="expectations JSON path")

    p_chk = sub.add_parser("check", help="compare state against expectations")
    p_chk.add_argument("--expect", required=True, help="expectations JSON path")
    p_chk.add_argument("--json", action="store_true")

    p_diff = sub.add_parser("diff", help="compare two files or directory trees")
    p_diff.add_argument("--a", required=True)
    p_diff.add_argument("--b", required=True)
    p_diff.add_argument("--max-hunks", type=int, default=3)
    p_diff.add_argument("--max-hunk-chars", type=int, default=400)
    p_diff.add_argument("--log-dir", default=".dsh-batch-logs")

    args = ap.parse_args(argv)

    if args.cmd == "snapshot":
        data = snapshot(args.paths)
        try:
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
        except OSError as exc:
            print("fatal: cannot write %s: %s" % (args.out, exc), file=sys.stderr)
            return 1
        print("[snapshot] recorded %d path(s) -> %s"
              % (len(data["entries"]), args.out))
        for e in data["entries"]:
            print("  %s exists=%s %s"
                  % (e["path"], e["exists"],
                     ("sha256=%s..." % e["sha256"][:12]) if "sha256" in e
                     else ("files=%d" % e["files"]) if "files" in e else ""))
        return 0

    if args.cmd == "check":
        if not os.path.isfile(args.expect):
            print("fatal: expectations not found: %s" % args.expect,
                  file=sys.stderr)
            return 1
        results = check(args.expect)
        fails = [r for r in results if r["status"] == "FAIL"]
        if args.json:
            print(json.dumps({"expect": args.expect,
                              "pass": len(results) - len(fails),
                              "fail": len(fails),
                              "results": results}, indent=2,
                             ensure_ascii=False))
        else:
            print("[check] %s -> %d pass, %d fail"
                  % (args.expect, len(results) - len(fails), len(fails)))
            for r in results:
                print("  %-5s %s: %s" % (r["status"], r["path"], r["detail"]))
        return 2 if fails else 0

    # diff
    for p in (args.a, args.b):
        if not os.path.exists(p):
            print("fatal: path not found: %s" % p, file=sys.stderr)
            return 1
    report, human_lines, log_path = diff_paths(
        args.a, args.b, args.max_hunks, args.max_hunk_chars, args.log_dir)
    print("[diff] %s vs %s -> %s" % (args.a, args.b, report["result"]))
    if "added_lines" in report:
        print("lines: +%d / -%d" % (report["added_lines"],
                                    report["removed_lines"]))
    if "only_in_a" in report:
        print("only in A (%d): %s" % (len(report["only_in_a"]),
                                      report["only_in_a"][:10]))
        print("only in B (%d): %s" % (len(report["only_in_b"]),
                                      report["only_in_b"][:10]))
        print("changed (%d): %s" % (len(report["changed"]),
                                    report["changed"][:10]))
    print("preview:")
    for line in human_lines[1:]:
        print("  " + line)
    if log_path:
        print("full diff log: %s" % log_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
