#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""loop_guard.py - Detect unproductive repetition and govern retries.

Part of the ``deepseek-harness-batch-executor`` skill for DeepSeek Harness.

An agent that keeps retrying the same failing operation, or keeps
"verifying" without any state change, burns tokens and time without
progress. This tool keeps a small JSON state file per workspace and answers
one question:

    "Should I execute this action again right now?"

Usage pattern for the model:

  1. check  --action KEY                       -> PROCEED / WARN / STOP
  2. ...perform the action (only if PROCEED)...
  3. record --action KEY --result ok|fail \
             [--evidence sha256 | --evidence-file PATH]
  4. repeat; ALWAYS honor STOP verdicts and report the reason.

State-change detection: record an evidence hash (e.g. sha256 of the target
file, a `git status` digest, a row count). If two consecutive attempts share
the same evidence hash, the world did not change - retrying blindly would be
a loop, so the guard says STOP with the reason.

State-change false positives: a successful no-op (same evidence after "ok")
raises WARN: verify before claiming success.

Exit codes: check -> 0 = PROCEED, 2 = STOP (WARN also 0 with a warning);
record/status/reset -> 0 ok, 1 = error.

Stdlib only. Python 3.8+.
"""

import argparse
import datetime
import hashlib
import json
import os
import sys

MAX_ATTEMPTS_DEFAULT = 3


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def load_state(path):
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (ValueError, OSError):
            pass
    return {"actions": {}}


def save_state(path, state):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, ensure_ascii=False)


def append_journal(journal_path, entry):
    os.makedirs(os.path.dirname(os.path.abspath(journal_path)),
                exist_ok=True)
    with open(journal_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def verdict_for(action, state, max_attempts):
    """Return (verdict, reason). verdict in {PROCEED, WARN, STOP}."""
    a = state["actions"].get(action)
    if not a:
        return "PROCEED", "first attempt for this action"
    hist = a.get("history", [])
    last = hist[-1] if hist else {}
    prev = hist[-2] if len(hist) >= 2 else None

    if a.get("last_result") == "fail":
        if a.get("attempts", 0) >= max_attempts:
            return ("STOP", "retry limit reached: %d failed attempt(s) "
                            "(max_attempts=%d). Stop and report the failure "
                            "with evidence instead of retrying."
                    % (a.get("attempts", 0), max_attempts))
        if (last.get("evidence") and prev
                and prev.get("evidence") == last["evidence"]):
            return ("STOP", "two consecutive failures with IDENTICAL evidence "
                            "(%s...): the world did not change between "
                            "attempts, so retrying is a loop. Diagnose or "
                            "change the approach first."
                    % last["evidence"][:12])
        return ("PROCEED", "previous attempt failed but the evidence changed; "
                           "a retry is justified")
    # last result ok
    if (last.get("evidence") and prev
            and prev.get("evidence") == last["evidence"]):
        return ("WARN", "last attempt reported success but the evidence hash "
                        "did NOT change: the operation may have been a no-op. "
                        "Verify the actual effect before reporting success.")
    return ("PROCEED", "previous attempt succeeded; re-execution is allowed "
                       "but confirm it is actually needed")


def cmd_check(args):
    state = load_state(args.state_file)
    verdict, reason = verdict_for(args.action, state, args.max_attempts)
    a = state["actions"].get(args.action)
    info = {
        "action": args.action,
        "verdict": verdict,
        "reason": reason,
        "attempts": a.get("attempts", 0) if a else 0,
        "last_result": a.get("last_result") if a else None,
    }
    if args.json:
        print(json.dumps(info, indent=2, ensure_ascii=False))
    else:
        print("[loop-guard] action=%r verdict=%s" % (args.action, verdict))
        print("reason: %s" % reason)
    return 0 if verdict in ("PROCEED", "WARN") else 2


def cmd_record(args):
    evidence = args.evidence
    if args.evidence_file:
        evidence = sha256_file(args.evidence_file)
        if evidence is None:
            print("error: cannot hash evidence file: %s" % args.evidence_file,
                  file=sys.stderr)
            return 1
    state = load_state(args.state_file)
    ts = utc_now()
    a = state["actions"].setdefault(args.action, {
        "attempts": 0, "last_result": None, "last_evidence": None,
        "consecutive_failures": 0, "history": []})
    a["attempts"] += 1
    if args.result == "fail":
        a["consecutive_failures"] = a.get("consecutive_failures", 0) + 1
    else:
        a["consecutive_failures"] = 0
    a["last_result"] = args.result
    a["last_evidence"] = evidence
    a["last_ts"] = ts
    a["history"].append({"ts": ts, "result": args.result,
                         "evidence": evidence})
    a["history"] = a["history"][-20:]
    save_state(args.state_file, state)
    append_journal(args.journal_file, {"ts": ts, "action": args.action,
                                       "result": args.result,
                                       "evidence": evidence})
    print("[loop-guard] recorded action=%r result=%s attempts=%d "
          "consecutive_failures=%d evidence=%s"
          % (args.action, args.result, a["attempts"],
             a["consecutive_failures"],
             (evidence[:12] + "...") if evidence else "(none)"))
    return 0


def cmd_status(args):
    state = load_state(args.state_file)
    actions = state.get("actions", {})
    if not actions:
        print("[loop-guard] no actions recorded yet")
        return 0
    print("[loop-guard] %d action(s) tracked" % len(actions))
    for key in sorted(actions):
        a = actions[key]
        hist = a.get("history", [])
        stuck = (a.get("consecutive_failures", 0) >= 2)
        if len(hist) >= 2 and hist[-1].get("evidence") \
                and hist[-1]["evidence"] == hist[-2].get("evidence"):
            stuck = True
        flag = " STUCK?" if stuck else ""
        print("  %-40s attempts=%-3d last=%-5s consec_fail=%-3d%s"
              % (key[:40], a.get("attempts", 0), a.get("last_result"),
                 a.get("consecutive_failures", 0), flag))
        if stuck:
            print("    -> no progress detected; do not retry without new "
                  "evidence or a changed approach")
    return 0


def cmd_reset(args):
    state = load_state(args.state_file)
    if args.action in state["actions"]:
        del state["actions"][args.action]
        save_state(args.state_file, state)
        print("[loop-guard] reset action=%r" % args.action)
    else:
        print("[loop-guard] action=%r was not tracked; nothing to reset"
              % args.action)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Guard against unproductive retry loops: check before "
                    "acting, record after acting, honor STOP.")
    ap.add_argument("--state-file", default=".dsh-loop-guard/state.json",
                    help="state file path (default: .dsh-loop-guard/state.json)")
    ap.add_argument("--journal-file", default=".dsh-loop-guard/journal.jsonl",
                    help="append-only journal path "
                         "(default: .dsh-loop-guard/journal.jsonl)")
    ap.add_argument("--max-attempts", type=int, default=MAX_ATTEMPTS_DEFAULT,
                    help="retry limit per action (default: 3)")
    ap.add_argument("--json", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("check", help="ask whether an action may proceed")
    p.add_argument("--action", required=True,
                   help="stable key identifying the operation, e.g. "
                        "'fix:src/db.py:line42'")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("record", help="record the outcome of an action")
    p.add_argument("--action", required=True)
    p.add_argument("--result", required=True, choices=["ok", "fail"])
    p.add_argument("--evidence", help="hash/digest describing world state "
                                      "after the action")
    p.add_argument("--evidence-file", help="file whose sha256 becomes the "
                                           "evidence hash")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("status", help="show tracked actions and stuck flags")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("reset", help="forget one action's history")
    p.add_argument("--action", required=True)
    p.set_defaults(func=cmd_reset)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
