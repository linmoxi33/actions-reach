import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import actions_reach as ar  # noqa: E402

BIN = os.path.join(ROOT, "bin", "actions-reach")
PIPE = os.path.join(ROOT, "examples", "in", "pipeline.yml")
SCEN = os.path.join(ROOT, "examples", "in", "scenario.yml")


def run(*args):
    p = subprocess.run([sys.executable, BIN] + list(args), capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def write_tmp(text):
    fd, path = tempfile.mkstemp(suffix=".yml")
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    return path


class TestParser(unittest.TestCase):
    def test_nested_map_and_sequence(self):
        doc = ar.parse_yaml("a:\n  b: 1\n  c:\n    - x\n    - y: 2\n")
        self.assertEqual(doc["a"]["b"], 1)
        self.assertEqual(doc["a"]["c"][0], "x")
        self.assertEqual(doc["a"]["c"][1]["y"], 2)

    def test_block_scalar_and_flow_list(self):
        doc = ar.parse_yaml("jobs:\n  a:\n    run: |\n      echo one\n      echo two\n"
                            "    on: [push, pull_request]\n")
        self.assertEqual(doc["jobs"]["a"]["run"], "echo one\necho two")
        self.assertEqual(doc["jobs"]["a"]["on"], ["push", "pull_request"])

    def test_matrix_cartesian_exclude_include(self):
        doc = ar.parse_yaml(
            "on: push\njobs:\n  a:\n    strategy:\n      matrix:\n        os: [l, m]\n"
            "        v: [1, 2]\n        exclude:\n          - os: m\n            v: 2\n"
            "        include:\n          - os: l\n            extra: x\n"
            "    runs-on: ubuntu-latest\n    steps: []\n")
        legs = ar.Model(doc).legs(ar.Model(doc).jobs["a"])
        self.assertEqual(len(legs), 3)
        self.assertTrue(any(leg.get("extra") == "x" for leg in legs))


class TestRules(unittest.TestCase):
    def test_self_test_every_rule_proven(self):
        rc, out, _ = run("--self-test")
        self.assertEqual(rc, 0)
        self.assertIn("15/15 proven", out)

    def test_clean_workflow_exits_zero(self):
        path = write_tmp("on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
                         "    steps: [{run: echo hi}]\n")
        rc, out, _ = run(path)
        self.assertEqual(rc, 0, out)

    def test_dead_job_detected_with_exit_code_1(self):
        rc, out, _ = run(PIPE)
        self.assertEqual(rc, 1)
        self.assertIn("AR001", out)
        self.assertIn("AR004", out)

    def test_missing_trigger_detected(self):
        rc, out, _ = run(PIPE)
        self.assertIn("AR012", out)

    def test_unknown_need_is_reported(self):
        path = write_tmp("on: push\njobs:\n  a:\n    needs: ghost\n"
                         "    runs-on: ubuntu-latest\n    steps: []\n")
        rc, out, _ = run(path)
        self.assertEqual(rc, 1)
        self.assertIn("AR002", out)

    def test_unknown_output_does_not_claim_dead(self):
        path = write_tmp("on: push\njobs:\n  c:\n    runs-on: ubuntu-latest\n"
                         "    outputs:\n      r: ${{ steps.s.outputs.r }}\n    steps: []\n"
                         "  b:\n    needs: c\n    if: needs.c.outputs.r == 'true'\n"
                         "    runs-on: ubuntu-latest\n    steps: []\n")
        rc, _, _ = run(path)
        self.assertEqual(rc, 0)  # no false "dead code" claim when the value is unknown


class TestCliContract(unittest.TestCase):
    def test_json_is_parsable_and_has_summary(self):
        rc, out, _ = run("--json", PIPE)
        data = json.loads(out)
        self.assertEqual(rc, 1)
        self.assertEqual(data["summary"]["jobs"], 4)
        self.assertTrue(all("rule" in f for f in data["findings"]))

    def test_usage_error_without_files(self):
        rc, _, err = run()
        self.assertEqual(rc, 2)
        self.assertIn("actions-reach", err + run("--nope")[2])

    def test_unreadable_input_exits_2(self):
        rc, _, err = run("/tmp/definitely-not-here-42.yml")
        self.assertEqual(rc, 2)

    def test_scenario_verdicts_and_witnesses(self):
        rc, out, _ = run("--json", "--trigger", "push", "--output", "check.should_release=true",
                         "--assume", "check=success,build=success", SCEN)
        self.assertEqual(rc, 0)
        jobs = {e["job"]: e for e in json.loads(out)["scenario"]["jobs"][SCEN]}
        self.assertEqual(jobs["publish"]["verdict"], "runs")
        self.assertLess(jobs["publish"]["witness_size"], jobs["publish"]["witness_full"])

    def test_witness_is_one_minimal(self):
        _, out, _ = run("--json", "--trigger", "push", "--output", "check.should_release=true",
                        "--assume", "check=success,build=success", SCEN)
        entry = [e for e in json.loads(out)["scenario"]["jobs"][SCEN] if e["job"] == "build"][0]
        model = ar.Model(ar.parse_yaml(open(SCEN).read()))
        witness, verdict = entry["witness"], entry["verdict"]
        self.assertEqual(ar.simulate(model, "push", witness).get("build"), verdict)
        for fact in witness:  # dropping any single fact must break the verdict
            smaller = [f for f in witness if f != fact]
            self.assertNotEqual(ar.simulate(model, "push", smaller).get("build"), verdict)

    def test_self_test_json_mode(self):
        rc, out, _ = run("--self-test", "--json")
        data = json.loads(out)
        self.assertEqual(rc, 0)
        self.assertEqual(data["passed"], data["total"])
        self.assertTrue(all(r["diff_lines"] == 1 for r in data["self_test"]))


if __name__ == "__main__":
    unittest.main()
