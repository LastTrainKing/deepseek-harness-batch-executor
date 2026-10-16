#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""batch_exec.py - Programmatic batch executor for deterministic file operations.

Part of the ``deepseek-harness-batch-executor`` skill for DeepSeek Harness.

Philosophy: the model decides *what* to do; this executor performs a whole
pre-planned sequence in one programmatic pass and returns a compact receipt.
Full per-operation detail (results, errors, timings) is written to a local
JSON log; only summary counts, verification results and failure evidence are
printed to stdout.

Supported operations (entries of ``operations`` in the plan JSON):

- copy   : copy a file or a directory tree (respects the overwrite policy)
- mkdir  : create a directory (idempotent)
- move   : move/rename a file or directory (never overwrites)
- remove : delete a file or directory. REQUIRES ``"requires_approval": true``
           in the operation AND ``--approve`` on the command line.
- expect : assert the state of a path (exists / sha256)

Verification (entries of ``verify``, run after all operations):

- count     : number of files matching a glob equals an expected value
- hash      : sha256 of two paths (files or trees) are equal
- tree-hash : two directory trees contain the same files with the same hashes

Exit codes: 0 = success (including already-satisfied work), 2 = partial
failure or refusal, 1 = fatal error (bad plan, unwritable log directory).

Stdlib only. Python 3.8+.
"""

import argparse
import datetime
import glob
import hashlib
import json
import os
import re
import shutil
import sys
import time

POLICIES = ("never", "if-different", "always")


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


def copy_tree_with_policy(src, dst, policy):
    """Copy a directory tree honoring the overwrite policy.

    Returns (copied_files, already_identical_files).
    """
    copied = 0
    already = 0
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames.sort()
        rel_dir = os.path.relpath(dirpath, src)
        target_dir = dst if rel_dir == "." else os.path.join(dst, rel_dir)
        os.makedirs(target_dir, exist_ok=True)
        for fn in sorted(filenames):
            s = os.path.join(dirpath, fn)
            t = os.path.join(target_dir, fn)
            if os.path.exists(t):
                if policy == "never":
                    raise FileExistsError(
                        "destination exists and overwrite=never: %s" % t)
                if policy == "if-different" and sha256_file(t) == sha256_file(s):
                    already += 1
                    continue
            shutil.copy2(s, t)
            copied += 1
    return copied, already


class Executor(object):
    def __init__(self, plan, approve=False, dry_run=False):
        self.plan = plan
        self.approve = approve
        self.dry_run = dry_run
        self.results = []
        self.default_policy = plan.get("overwrite", "if-different")
        if self.default_policy not in POLICIES:
            raise ValueError("invalid overwrite policy: %s" % self.default_policy)

    # ---- pre-check -----------------------------------------------------
    def pre_check(self):
        issues = []
        ops = self.plan.get("operations", [])
        for i, op in enumerate(ops):
            kind = op.get("op")
            if kind not in ("copy", "mkdir", "move", "remove", "expect"):
                issues.append("op[%d]: unknown op %r" % (i, kind))
                continue
            if kind in ("copy", "move") and not op.get("src"):
                issues.append("op[%d]: missing 'src'" % i)
            if kind in ("mkdir", "expect", "remove") and not op.get("path"):
                issues.append("op[%d]: missing 'path'" % i)
            if kind == "remove" and not op.get("requires_approval"):
                issues.append(
                    "op[%d]: 'remove' without requires_approval will be refused" % i)
            if kind in ("copy", "move"):
                src = op.get("src")
                if src and not os.path.exists(src):
                    issues.append("op[%d]: source not found: %s" % (i, src))
        return issues

    # ---- execution -----------------------------------------------------
    def run_op(self, op):
        kind = op.get("op")
        oid = op.get("id") or ("%s-%d" % (kind or "op", len(self.results) + 1))
        rec = {"id": oid, "op": kind}
        try:
            rec.update(self._dispatch(kind, op))
        except Exception as exc:  # noqa: BLE001 - one bad op must not kill the batch
            rec["result"] = "failed"
            rec["detail"] = "%s: %s" % (type(exc).__name__, exc)
        self.results.append(rec)
        return rec

    def _dispatch(self, kind, op):
        if self.dry_run:
            return {"result": "planned",
                    "detail": "dry-run, not executed"}
        if kind == "copy":
            return self._copy(op)
        if kind == "mkdir":
            return self._mkdir(op)
        if kind == "move":
            return self._move(op)
        if kind == "remove":
            return self._remove(op)
        if kind == "expect":
            return self._expect(op)
        raise ValueError("unknown op: %r" % kind)

    def _copy(self, op):
        src = op["src"]
        dst = op["dst"]
        policy = op.get("overwrite", self.default_policy)
        if policy not in POLICIES:
            raise ValueError("invalid overwrite policy: %s" % policy)
        if not os.path.exists(src):
            raise FileNotFoundError("source not found: %s" % src)
        if os.path.isdir(src):
            copied, already = copy_tree_with_policy(src, dst, policy)
            return {"result": "ok",
                    "detail": "tree copied %s -> %s (%d file(s) copied, %d already identical)"
                              % (src, dst, copied, already)}
        # single file; allow dst to be an existing directory
        if os.path.isdir(dst):
            dst = os.path.join(dst, os.path.basename(src))
        if os.path.exists(dst):
            if policy == "never":
                raise FileExistsError(
                    "destination exists (overwrite=never): %s" % dst)
            if policy == "if-different" and sha256_file(dst) == sha256_file(src):
                return {"result": "already_satisfied",
                        "detail": "identical content already present, skipped: %s" % dst}
        parent = os.path.dirname(os.path.abspath(dst))
        os.makedirs(parent, exist_ok=True)
        shutil.copy2(src, dst)
        return {"result": "ok",
                "detail": "copied %s -> %s (%d bytes)"
                          % (src, dst, os.path.getsize(dst))}

    def _mkdir(self, op):
        path = op["path"]
        if os.path.isdir(path):
            return {"result": "already_satisfied",
                    "detail": "directory already exists: %s" % path}
        os.makedirs(path, exist_ok=True)
        return {"result": "ok", "detail": "created directory: %s" % path}

    def _move(self, op):
        src = op["src"]
        dst = op["dst"]
        if not os.path.exists(src):
            raise FileNotFoundError("source not found: %s" % src)
        if os.path.exists(dst):
            raise FileExistsError(
                "destination exists (move never overwrites): %s" % dst)
        parent = os.path.dirname(os.path.abspath(dst))
        os.makedirs(parent, exist_ok=True)
        shutil.move(src, dst)
        return {"result": "ok", "detail": "moved %s -> %s" % (src, dst)}

    def _remove(self, op):
        path = op["path"]
        if not op.get("requires_approval"):
            return {"result": "refused",
                    "detail": "'remove' must declare requires_approval: true "
                              "in the plan (op %r)" % op.get("id")}
        if not self.approve:
            return {"result": "refused",
                    "detail": "'remove' requires --approve on the command line; "
                              "destructive operations are never auto-approved"}
        if not os.path.exists(path):
            return {"result": "already_satisfied",
                    "detail": "path did not exist, nothing to remove: %s" % path}
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
        return {"result": "ok", "detail": "removed %s" % path}

    def _expect(self, op):
        path = op["path"]
        exp = op.get("expect", {})
        if exp.get("exists") is False:
            if os.path.exists(path):
                raise AssertionError("expected absent, but it exists: %s" % path)
            return {"result": "ok", "detail": "confirmed absent: %s" % path}
        if not os.path.exists(path):
            raise FileNotFoundError("expected path missing: %s" % path)
        if "sha256" in exp:
            if os.path.isdir(path):
                raise AssertionError(
                    "sha256 expectation is only supported for files: %s" % path)
            actual = sha256_file(path)
            if actual != exp["sha256"]:
                raise AssertionError(
                    "sha256 mismatch for %s: expected %s..., actual %s..."
                    % (path, exp["sha256"][:12], actual[:12]))
        return {"result": "ok", "detail": "expectation satisfied: %s" % path}

    # ---- verification --------------------------------------------------
    def run_verify(self):
        out = []
        for v in self.plan.get("verify", []):
            vtype = v.get("type")
            try:
                if vtype == "count":
                    pattern = v["pattern"]
                    matches = [p for p in glob.glob(pattern, recursive=True)
                               if os.path.isfile(p)]
                    ok = len(matches) == v["expect"]
                    out.append({"type": "count",
                                "status": "pass" if ok else "fail",
                                "detail": "glob %r matched %d file(s), expected %d"
                                          % (pattern, len(matches), v["expect"])})
                elif vtype == "hash":
                    a, b = v["src"], v["dst"]
                    if os.path.isfile(a) and os.path.isfile(b):
                        ok = sha256_file(a) == sha256_file(b)
                        detail = "%s vs %s: %s" % (
                            a, b, "identical" if ok else "DIFFER")
                    else:
                        ha, hb = path_hashes(a), path_hashes(b)
                        ok = ha == hb
                        detail = "%s vs %s: %s (%d vs %d file(s))" % (
                            a, b, "identical" if ok else "DIFFER",
                            len(ha), len(hb))
                    out.append({"type": "hash",
                                "status": "pass" if ok else "fail",
                                "detail": detail})
                elif vtype == "tree-hash":
                    a, b = v["src"], v["dst"]
                    ha, hb = path_hashes(a), path_hashes(b)
                    ok = ha == hb
                    if ok:
                        detail = "trees identical (%d file(s))" % len(ha)
                    else:
                        only_a = sorted(set(ha) - set(hb))
                        only_b = sorted(set(hb) - set(ha))
                        diff = sorted(k for k in set(ha) & set(hb)
                                      if ha[k] != hb[k])
                        detail = ("MISMATCH only-in-src=%s only-in-dst=%s "
                                  "differing=%s" % (only_a[:5], only_b[:5], diff[:5]))
                    out.append({"type": "tree-hash",
                                "status": "pass" if ok else "fail",
                                "detail": detail})
                else:
                    out.append({"type": str(vtype), "status": "fail",
                                "detail": "unknown verify type: %r" % vtype})
            except Exception as exc:  # noqa: BLE001
                out.append({"type": str(vtype), "status": "fail",
                            "detail": "%s: %s" % (type(exc).__name__, exc)})
        return out


def safe_name(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(text))[:60] or "plan"


def build_counts(results):
    counts = {"total": len(results)}
    for r in results:
        key = r.get("result", "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts


def overall_status(counts):
    bad = counts.get("failed", 0) + counts.get("refused", 0)
    if bad == 0:
        return "success"
    good = counts.get("ok", 0) + counts.get("already_satisfied", 0)
    return "partial" if good > 0 else "failed"


def print_receipt(receipt, as_json):
    if as_json:
        print(json.dumps(receipt, indent=2, ensure_ascii=False))
        return
    c = receipt["counts"]
    print("[receipt] plan=%s status=%s elapsed_ms=%d"
          % (receipt["plan_id"], receipt["status"], receipt["elapsed_ms"]))
    print("counts: total=%d ok=%d already_satisfied=%d failed=%d refused=%d planned=%d"
          % (c.get("total", 0), c.get("ok", 0), c.get("already_satisfied", 0),
             c.get("failed", 0), c.get("refused", 0), c.get("planned", 0)))
    print("operations:")
    for r in receipt["results"]:
        print("  %-24s %-8s %-18s %s"
              % (r.get("id", "?"), r.get("op", "?"), r.get("result", "?"),
                 r.get("detail", "")))
    if receipt["verify"]:
        print("verify:")
        for v in receipt["verify"]:
            print("  %-12s %-5s %s" % (v["type"], v["status"], v["detail"]))
    else:
        print("verify: (none declared in plan)")
    if receipt.get("pre_check_issues"):
        print("pre-check issues:")
        for it in receipt["pre_check_issues"]:
            print("  - %s" % it)
    print("full log: %s" % receipt["log_path"])


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Execute a planned batch of deterministic file operations "
                    "with local verification and a compact receipt.")
    ap.add_argument("--plan", required=True,
                    help="path to the JSON plan file")
    ap.add_argument("--dry-run", action="store_true",
                    help="run pre-checks only; report what would happen")
    ap.add_argument("--approve", action="store_true",
                    help="approve operations that declare requires_approval "
                         "(currently only 'remove')")
    ap.add_argument("--log-dir", default=".dsh-batch-logs",
                    help="directory for full JSON logs (default: .dsh-batch-logs)")
    ap.add_argument("--fail-fast", action="store_true",
                    help="skip remaining operations after the first failure")
    ap.add_argument("--json", action="store_true",
                    help="print the receipt as JSON instead of text")
    args = ap.parse_args(argv)

    # 1. load plan
    try:
        with open(args.plan, "r", encoding="utf-8") as fh:
            plan = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        print("fatal: cannot load plan %s: %s" % (args.plan, exc), file=sys.stderr)
        return 1
    if not isinstance(plan.get("operations"), list) or not plan["operations"]:
        print("fatal: plan has no 'operations' list", file=sys.stderr)
        return 1
    plan_id = plan.get("plan_id") or os.path.splitext(
        os.path.basename(args.plan))[0]

    try:
        ex = Executor(plan, approve=args.approve, dry_run=args.dry_run)
    except ValueError as exc:
        print("fatal: %s" % exc, file=sys.stderr)
        return 1

    # 2. pre-check (issues are reported; the batch still runs so the model
    #    learns the real failure surface instead of a guessed one)
    issues = ex.pre_check()
    for it in issues:
        print("[pre-check] %s" % it)

    # 3. execute
    started = time.time()
    if args.dry_run:
        for op in plan["operations"]:
            ex.results.append({"id": op.get("id") or "?",
                               "op": op.get("op"),
                               "result": "planned",
                               "detail": "dry-run, not executed"})
    else:
        for op in plan["operations"]:
            rec = ex.run_op(op)
            if args.fail_fast and rec["result"] in ("failed", "refused"):
                for rest in plan["operations"][len(ex.results):]:
                    ex.results.append({"id": rest.get("id") or "?",
                                       "op": rest.get("op"),
                                       "result": "skipped",
                                       "detail": "skipped due to --fail-fast"})
                break

    # 4. verify
    verify = ex.run_verify()
    elapsed_ms = int((time.time() - started) * 1000)

    # 5. write the full log (audit trail + on-demand deep read)
    try:
        os.makedirs(args.log_dir, exist_ok=True)
        log_path = os.path.join(
            args.log_dir,
            "%s-%s.json" % (safe_name(plan_id),
                            time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())))
        log_obj = {
            "plan_id": plan_id,
            "started_at": utc_now(),
            "elapsed_ms": elapsed_ms,
            "dry_run": bool(args.dry_run),
            "approve": bool(args.approve),
            "plan": plan,
            "pre_check_issues": issues,
            "results": ex.results,
            "verify": verify,
        }
        with open(log_path, "w", encoding="utf-8") as fh:
            json.dump(log_obj, fh, indent=2, ensure_ascii=False)
    except OSError as exc:
        print("fatal: cannot write log to %s: %s" % (args.log_dir, exc),
              file=sys.stderr)
        return 1

    counts = build_counts(ex.results)
    receipt = {
        "plan_id": plan_id,
        "status": overall_status(counts),
        "started_at": log_obj["started_at"],
        "elapsed_ms": elapsed_ms,
        "dry_run": bool(args.dry_run),
        "counts": counts,
        "results": ex.results,
        "verify": verify,
        "pre_check_issues": issues,
        "log_path": log_path,
    }
    print_receipt(receipt, args.json)

    bad = counts.get("failed", 0) + counts.get("refused", 0)
    if bad > 0:
        return 2
    vfail = [v for v in verify if v["status"] != "pass"]
    if vfail and not args.dry_run:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
