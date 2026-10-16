#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_tests.py - Integration tests for deepseek-harness-batch-executor.

Every test runs the real scripts as subprocesses against real fixtures in a
temporary directory. No network, no pip install, no external services.

Scenarios covered (mapped to the project's test plan):

  T1  batch copy of many files + count and content-hash verification
  T2  idempotent re-run (uncertain result -> check state, no duplicate work)
  T3  batch search: summary to stdout, full results kept in JSONL log,
      on-demand deep read via --show-id
  T4  modify a file, then verify the actual diff (hash evidence)
  T5  test/command run: failure summary extracted, full log kept,
      --show-failure deep read
  T6  partial failure: receipt must NOT report success; failures are
      locatable (exit code 2, counts accurate)
  T7  destructive op refused without approval; succeeds with --approve
  T8  loop guard: repeated identical failures -> STOP; no-progress WARN
  T9  skill metadata compliance with the DeepSeek Harness skill spec
      (name regex, required frontmatter fields, section tags, layout)

Run:  python tests/run_tests.py [-v]
Exit: 0 = all passed, 1 = at least one failed.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SCRIPTS = os.path.join(REPO, "scripts")
PYTHON = sys.executable

BATCH_EXEC = os.path.join(SCRIPTS, "batch_exec.py")
SEARCH = os.path.join(SCRIPTS, "search_summary.py")
VERIFY = os.path.join(SCRIPTS, "verify_state.py")
TESTSUM = os.path.join(SCRIPTS, "test_summary.py")
LOOPGUARD = os.path.join(SCRIPTS, "loop_guard.py")


def run(args, **kw):
    return subprocess.run([PYTHON] + args, capture_output=True, text=True,
                          **kw)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dsh-skill-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, relpath, content):
        full = os.path.join(self.tmp, relpath)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)
        return full

    def path(self, relpath):
        return os.path.join(self.tmp, relpath)


class T1BatchCopyVerify(Base):
    """T1: batch copy many files, verify count + content consistency."""

    def test_batch_copy_and_verify(self):
        n = 20
        for i in range(n):
            self.write("templates/file-%03d.conf" % i,
                       "key%d=value%d\n" % (i, i) + "padding\n" * 30)
        plan = {
            "plan_id": "t1-copy",
            "description": "copy 20 config templates",
            "overwrite": "if-different",
            "operations": [
                {"id": "op-%03d" % i, "op": "copy",
                 "src": self.path("templates/file-%03d.conf" % i),
                 "dst": self.path("build/file-%03d.conf" % i)}
                for i in range(n)
            ],
            "verify": [
                {"type": "count",
                 "pattern": self.path("build/*.conf"), "expect": n},
                {"type": "tree-hash",
                 "src": self.path("templates"),
                 "dst": self.path("build")},
            ],
        }
        plan_path = self.path("plan.json")
        with open(plan_path, "w", encoding="utf-8") as fh:
            json.dump(plan, fh)

        proc = run([BATCH_EXEC, "--plan", plan_path,
                    "--log-dir", self.path("logs")])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("status=success", proc.stdout)
        self.assertIn("ok=%d" % n, proc.stdout)
        self.assertIn("failed=0", proc.stdout)
        # both verifications passed
        self.assertEqual(proc.stdout.count("pass"), 2, proc.stdout)
        # files really exist with identical content
        for i in range(n):
            a = open(self.path("templates/file-%03d.conf" % i), "rb").read()
            b = open(self.path("build/file-%03d.conf" % i), "rb").read()
            self.assertEqual(a, b)
        # full log written
        logs = os.listdir(self.path("logs"))
        self.assertEqual(len(logs), 1)
        with open(os.path.join(self.path("logs"), logs[0]),
                  encoding="utf-8") as fh:
            log = json.load(fh)
        self.assertEqual(len(log["results"]), n)


class T2IdempotentRerun(Base):
    """T2: uncertain result -> check state first; re-run is a no-op."""

    def test_rerun_reports_already_satisfied(self):
        self.write("a.txt", "hello\n" * 10)
        plan = {
            "plan_id": "t2-copy",
            "operations": [
                {"id": "op-1", "op": "copy",
                 "src": self.path("a.txt"), "dst": self.path("b.txt")},
            ],
            "verify": [
                {"type": "hash", "src": self.path("a.txt"),
                 "dst": self.path("b.txt")},
            ],
        }
        plan_path = self.path("plan.json")
        with open(plan_path, "w", encoding="utf-8") as fh:
            json.dump(plan, fh)

        p1 = run([BATCH_EXEC, "--plan", plan_path,
                  "--log-dir", self.path("logs")])
        self.assertEqual(p1.returncode, 0, p1.stdout)
        self.assertIn("ok=1", p1.stdout)

        # re-run without knowing whether the first run persisted: the
        # executor must detect identical content instead of duplicating work
        p2 = run([BATCH_EXEC, "--plan", plan_path,
                  "--log-dir", self.path("logs")])
        self.assertEqual(p2.returncode, 0, p2.stdout)
        self.assertIn("already_satisfied=1", p2.stdout)
        self.assertIn("failed=0", p2.stdout)
        self.assertEqual(open(self.path("b.txt")).read(), "hello\n" * 10)


class T3SearchCompressed(Base):
    """T3: batch search returns a summary; full results stay in the log."""

    def test_search_summary_and_on_demand_read(self):
        # one file with many hits (one very long line), a few files with
        # single hits
        self.write("logs/big.log",
                   "ERROR line 0 " + "x" * 300 + "\n"
                   + "".join("ERROR line %d\n" % i for i in range(1, 120)))
        for f in ("app.log", "worker.log", "cache.log"):
            self.write("logs/%s" % f, "ERROR single hit in %s\n" % f)

        proc = run([SEARCH, "--root", self.path("logs"),
                    "--pattern", "ERROR", "--top", "5",
                    "--log-dir", self.path("logs-out")])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # summary: exact counts, bounded representative list
        self.assertIn("total_matches=123", proc.stdout)
        self.assertIn("files_touched=4", proc.stdout)
        rep_lines = [l for l in proc.stdout.splitlines()
                     if re.match(r"\s+m-\d{4} ", l)]
        self.assertEqual(len(rep_lines), 5)
        # full log exists and holds every match
        log_path = None
        for line in proc.stdout.splitlines():
            if "full results" in line:
                log_path = line.split(": ", 1)[1].strip()
        self.assertTrue(log_path and os.path.isfile(log_path), proc.stdout)
        with open(log_path, encoding="utf-8") as fh:
            entries = [json.loads(l) for l in fh if l.strip()]
        self.assertEqual(len(entries), 123)
        # summary payload is much smaller than the full log
        self.assertLess(len(proc.stdout),
                        os.path.getsize(log_path) // 2)
        # on-demand deep read returns the FULL untruncated line
        target = next(e for e in entries if e["text"].startswith("ERROR line 0"))
        show = run([SEARCH, "--show-id", target["id"], "--log-file", log_path])
        self.assertEqual(show.returncode, 0, show.stderr)
        entry = json.loads(show.stdout)
        self.assertEqual(entry["text"], target["text"])
        self.assertGreater(len(entry["text"]), 300)  # long line, complete
        # ...while the summary view was trimmed for that hit
        summary_line = [l for l in proc.stdout.splitlines()
                        if "ERROR line 0" in l]
        self.assertTrue(summary_line, proc.stdout)
        self.assertIn("[truncated, full text in log]", summary_line[0])


class T4ModifyAndVerify(Base):
    """T4: modify a file, then verify the actual diff with hash evidence."""

    def test_snapshot_check_diff(self):
        readme = self.write("README.md",
                            "# Title\n\noriginal content\n" + "filler\n" * 50)
        exp = self.path("expected.json")
        p = run([VERIFY, "snapshot", "--paths", readme, "--out", exp])
        self.assertEqual(p.returncode, 0, p.stderr)

        # check passes before modification
        p = run([VERIFY, "check", "--expect", exp])
        self.assertEqual(p.returncode, 0, p.stdout)

        # modify
        with open(readme, "a", encoding="utf-8") as fh:
            fh.write("appended section\n")

        # check now FAILS with hash evidence (the edit landed, provably)
        p = run([VERIFY, "check", "--expect", exp])
        self.assertEqual(p.returncode, 2, p.stdout)
        self.assertIn("FAIL", p.stdout)
        self.assertIn("sha256 mismatch", p.stdout)

        # diff shows exactly what changed
        before = self.write("before.md", "# Title\n\noriginal content\n")
        after = self.write("after.md", "# Title\n\nmodified content\n")
        p = run([VERIFY, "diff", "--a", before, "--b", after,
                 "--log-dir", self.path("logs")])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("differ", p.stdout)
        self.assertIn("+1 / -1", p.stdout)  # one added, one removed line
        self.assertTrue(os.path.isfile(os.path.join(
            self.path("logs"),
            [f for f in os.listdir(self.path("logs"))
             if f.startswith("diff-")][0])))


class T5TestSummary(Base):
    """T5: run a failing command; extract failure summary; keep full log."""

    def test_failure_summary_and_deep_read(self):
        script = self.write("fake_tests.py", """
import sys
print("===== 3 passed, 1 failed =====")
print("FAILED fake_tests.py::test_thing - AssertionError: expected 42 got 41")
print("Traceback (most recent call last):")
print('  File "fake_tests.py", line 7, in test_thing')
print("    assert 42 == 41")
print("E   AssertionError: expected 42 got 41")
sys.exit(1)
""")
        proc = run([TESTSUM, "--", PYTHON, script,
                    "--log-dir", self.path("logs")])
        self.assertEqual(proc.returncode, 2, proc.stdout)  # command failed
        self.assertIn("exit=1", proc.stdout)
        self.assertIn("failed=1", proc.stdout)
        self.assertIn("passed=3", proc.stdout)
        self.assertIn("AssertionError: expected 42 got 41", proc.stdout)
        # full log retained
        log_path = None
        for line in proc.stdout.splitlines():
            if "full output" in line:
                log_path = line.split(": ", 1)[1].strip()
        self.assertTrue(log_path and os.path.isfile(log_path))
        with open(log_path, encoding="utf-8") as fh:
            full = fh.read()
        self.assertIn("Traceback (most recent call last):", full)  # tail kept
        # on-demand deep read
        show = run([TESTSUM, "--show-failure", "1", "--log-file", log_path])
        self.assertEqual(show.returncode, 0, show.stderr)
        self.assertIn("AssertionError", show.stdout)

    def test_parser_unit_pytest_format(self):
        sys.path.insert(0, SCRIPTS)
        import test_summary as ts
        text = ("===== 2 failed, 5 passed in 0.42s =====\n"
                "FAILED tests/test_a.py::test_x - ValueError: bad\n"
                "FAILED tests/test_b.py::test_y - KeyError: 'k'\n")
        info = ts.parse_output(text)
        self.assertEqual(info["format"], "pytest")
        self.assertEqual(info["counts"]["failed"], 2)
        self.assertEqual(info["counts"]["passed"], 5)
        self.assertEqual(len(info["failures"]), 2)
        self.assertIn("tests/test_a.py", info["failures"][0])

    def test_parser_unit_go_format(self):
        sys.path.insert(0, SCRIPTS)
        import test_summary as ts
        text = "--- FAIL: TestAdd (0.00s)\n    add_test.go:12: 1 + 1 != 3\nFAIL\tpkg\t0.005s\n"
        info = ts.parse_output(text)
        self.assertEqual(info["format"], "go")
        self.assertEqual(info["failures"], ["TestAdd"])

    def test_parser_never_invents_counts(self):
        sys.path.insert(0, SCRIPTS)
        import test_summary as ts
        info = ts.parse_output("random command output\nno framework markers here\n")
        self.assertIn("generic", info["format"])
        self.assertEqual(info["counts"], {})


class T6PartialFailure(Base):
    """T6: partial failure must not be reported as success."""

    def test_partial_failure_receipt(self):
        self.write("good1.txt", "a\n" * 10)
        self.write("good2.txt", "b\n" * 10)
        plan = {
            "plan_id": "t6-partial",
            "operations": [
                {"id": "ok-1", "op": "copy",
                 "src": self.path("good1.txt"), "dst": self.path("out1.txt")},
                {"id": "bad-1", "op": "copy",
                 "src": self.path("missing-src.txt"),
                 "dst": self.path("out2.txt")},
                {"id": "ok-2", "op": "copy",
                 "src": self.path("good2.txt"), "dst": self.path("out3.txt")},
            ],
            "verify": [
                {"type": "count", "pattern": self.path("out*.txt"),
                 "expect": 3},  # only 2 will exist -> must FAIL
            ],
        }
        plan_path = self.path("plan.json")
        with open(plan_path, "w", encoding="utf-8") as fh:
            json.dump(plan, fh)
        proc = run([BATCH_EXEC, "--plan", plan_path,
                    "--log-dir", self.path("logs")])
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("status=partial", proc.stdout)
        self.assertIn("ok=2", proc.stdout)
        self.assertIn("failed=1", proc.stdout)
        # the failing op is named with its reason
        self.assertIn("bad-1", proc.stdout)
        self.assertIn("source not found", proc.stdout)
        # verification correctly reports the shortfall
        self.assertIn("fail", proc.stdout)
        self.assertIn("matched 2 file(s), expected 3", proc.stdout)

    def test_fail_fast_marks_skipped(self):
        self.write("good.txt", "x\n")
        plan = {
            "plan_id": "t6-failfast",
            "operations": [
                {"id": "bad", "op": "copy", "src": self.path("nope.txt"),
                 "dst": self.path("o1.txt")},
                {"id": "later", "op": "copy", "src": self.path("good.txt"),
                 "dst": self.path("o2.txt")},
            ],
        }
        plan_path = self.path("plan.json")
        with open(plan_path, "w", encoding="utf-8") as fh:
            json.dump(plan, fh)
        proc = run([BATCH_EXEC, "--plan", plan_path, "--fail-fast",
                    "--log-dir", self.path("logs")])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("skipped", proc.stdout)


class T7ApprovalGate(Base):
    """T7: destructive ops are refused without explicit approval."""

    def test_remove_requires_approval(self):
        target = self.write("doomed.txt", "bye\n" * 5)
        plan = {
            "plan_id": "t7-remove",
            "operations": [
                {"id": "rm-1", "op": "remove", "path": target,
                 "requires_approval": True},
            ],
        }
        plan_path = self.path("plan.json")
        with open(plan_path, "w", encoding="utf-8") as fh:
            json.dump(plan, fh)

        # without --approve: refused, file untouched
        p1 = run([BATCH_EXEC, "--plan", plan_path,
                  "--log-dir", self.path("logs")])
        self.assertEqual(p1.returncode, 2, p1.stdout)
        self.assertIn("refused", p1.stdout)
        self.assertIn("--approve", p1.stdout)
        self.assertTrue(os.path.exists(target))

        # with --approve: executed
        p2 = run([BATCH_EXEC, "--plan", plan_path, "--approve",
                  "--log-dir", self.path("logs")])
        self.assertEqual(p2.returncode, 0, p2.stdout)
        self.assertFalse(os.path.exists(target))

    def test_remove_without_declaration_refused(self):
        target = self.write("doomed2.txt", "bye\n")
        plan = {
            "plan_id": "t7-nodecl",
            "operations": [{"id": "rm", "op": "remove", "path": target}],
        }
        plan_path = self.path("plan.json")
        with open(plan_path, "w", encoding="utf-8") as fh:
            json.dump(plan, fh)
        p = run([BATCH_EXEC, "--plan", plan_path, "--approve",
                 "--log-dir", self.path("logs")])
        self.assertEqual(p.returncode, 2)
        self.assertIn("refused", p.stdout)
        self.assertTrue(os.path.exists(target))

    def test_overwrite_never_conflict(self):
        self.write("src.txt", "new content\n")
        self.write("dst.txt", "old content\n")
        plan = {
            "plan_id": "t7-overwrite",
            "overwrite": "never",
            "operations": [{"id": "cp", "op": "copy",
                            "src": self.path("src.txt"),
                            "dst": self.path("dst.txt")}],
        }
        plan_path = self.path("plan.json")
        with open(plan_path, "w", encoding="utf-8") as fh:
            json.dump(plan, fh)
        p = run([BATCH_EXEC, "--plan", plan_path,
                 "--log-dir", self.path("logs")])
        self.assertEqual(p.returncode, 2)
        self.assertIn("failed=1", p.stdout)
        self.assertEqual(open(self.path("dst.txt")).read(), "old content\n")


class T8LoopGuard(Base):
    """T8: repeated identical failures stop; no-progress is flagged."""

    def setUp(self):
        super().setUp()
        self.state = self.path("guard/state.json")
        self.journal = self.path("guard/journal.jsonl")
        self.evidence_file = self.write("target.py",
                                        "def broken():\n    return 1\n")

    def guard(self, *args):
        return run([LOOPGUARD, "--state-file", self.state,
                    "--journal-file", self.journal] + list(args))

    def test_repeated_failure_without_state_change_stops(self):
        action = "fix:target.py"
        p = self.guard("check", "--action", action)
        self.assertEqual(p.returncode, 0)
        self.assertIn("PROCEED", p.stdout)

        # attempt 1 fails, evidence recorded
        self.guard("record", "--action", action, "--result", "fail",
                   "--evidence-file", self.evidence_file)
        p = self.guard("check", "--action", action)
        self.assertEqual(p.returncode, 0)  # evidence may have changed

        # attempt 2 fails with the SAME evidence (file unchanged)
        self.guard("record", "--action", action, "--result", "fail",
                   "--evidence-file", self.evidence_file)
        p = self.guard("check", "--action", action)
        self.assertEqual(p.returncode, 2, p.stdout)
        self.assertIn("STOP", p.stdout)
        self.assertIn("did not change", p.stdout)

        # journal has both attempts
        with open(self.journal, encoding="utf-8") as fh:
            lines = [json.loads(l) for l in fh if l.strip()]
        self.assertEqual(len(lines), 2)

    def test_retry_limit_stops(self):
        action = "flaky:network-call"
        for _ in range(3):
            self.guard("record", "--action", action, "--result", "fail",
                       "--evidence", "hash-%d" % (_ + 1))
        p = self.guard("check", "--action", action)
        self.assertEqual(p.returncode, 2)
        self.assertIn("retry limit reached", p.stdout)

    def test_success_without_state_change_warns(self):
        action = "write:file.txt"
        self.guard("record", "--action", action, "--result", "ok",
                   "--evidence", "same-hash")
        self.guard("record", "--action", action, "--result", "ok",
                   "--evidence", "same-hash")
        p = self.guard("check", "--action", action)
        self.assertEqual(p.returncode, 0)
        self.assertIn("WARN", p.stdout)
        self.assertIn("no-op", p.stdout)

    def test_status_and_reset(self):
        self.guard("record", "--action", "a1", "--result", "fail",
                   "--evidence", "h1")
        self.guard("record", "--action", "a1", "--result", "fail",
                   "--evidence", "h1")
        p = self.guard("status")
        self.assertIn("a1", p.stdout)
        self.assertIn("STUCK?", p.stdout)
        p = self.guard("reset", "--action", "a1")
        self.assertEqual(p.returncode, 0)
        p = self.guard("status")
        self.assertNotIn("a1", p.stdout)


class T9SkillMetadata(Base):
    """T9: SKILL.md conforms to the DeepSeek Harness skill specification."""

    def setUp(self):
        super().setUp()
        with open(os.path.join(REPO, "SKILL.md"), encoding="utf-8") as fh:
            self.raw = fh.read()

    def parse_frontmatter(self):
        m = re.match(r"^---\n(.*?)\n---\n", self.raw, re.S)
        self.assertIsNotNone(m, "SKILL.md must start with YAML frontmatter")
        fm = {}
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                fm[k.strip()] = v.strip().strip('"').strip("'")
        return fm

    def test_required_frontmatter_fields(self):
        fm = self.parse_frontmatter()
        self.assertIn("name", fm)
        self.assertIn("description", fm)
        self.assertIn("whenToUse", fm)
        self.assertTrue(fm["description"].strip())
        self.assertTrue(fm["whenToUse"].strip())

    def test_name_matches_dsh_regex(self):
        fm = self.parse_frontmatter()
        self.assertRegex(fm["name"], r"^[a-z0-9][a-z0-9-]*$")
        # directory name should match the skill name (DSH convention)
        self.assertEqual(os.path.basename(REPO), fm["name"])

    def test_sections_and_layout(self):
        self.assertIn("<skill_instructions>", self.raw)
        self.assertIn("</skill_instructions>", self.raw)
        self.assertIn("<skill_resources>", self.raw)
        self.assertIn("Base directory for this skill", self.raw)
        # script references must resolve against the base directory
        for s in ("batch_exec.py", "search_summary.py", "verify_state.py",
                  "test_summary.py", "loop_guard.py"):
            self.assertTrue(os.path.isfile(os.path.join(SCRIPTS, s)),
                            "missing script: %s" % s)

    def test_skill_documents_trigger_and_boundaries(self):
        text = self.raw.lower()
        for topic in ("when to activate", "safety boundaries",
                      "stop conditions", "retry"):
            self.assertIn(topic, text,
                          "SKILL.md should document: %s" % topic)

    def test_repo_layout(self):
        for rel in ("README.md", "LICENSE", ".gitignore",
                    "references/ptc-playbook.md",
                    "examples/sample-plan.json",
                    "examples/bench_naive_vs_batched.py",
                    "tests/run_tests.py"):
            self.assertTrue(os.path.isfile(os.path.join(REPO, rel)),
                            "missing repo file: %s" % rel)


def main():
    unittest.main(verbosity=2)


if __name__ == "__main__":
    main()
