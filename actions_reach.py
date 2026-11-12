"""actions-reach: offline GitHub Actions job-reachability simulator.

Zero third-party dependencies. Parses the YAML subset used by workflow files,
evaluates JS-ish `if` expressions with three-valued logic (true / false /
unknown), expands `strategy.matrix`, and reports which jobs can never run,
which can be silently skipped, and *why* -- with a 1-minimal witness of
observed facts for every verdict.
"""
from __future__ import annotations

import json
import re
import sys

UNKNOWN = object()  # tri-state "cannot be determined offline"


# --------------------------------------------------------------------------- #
# Minimal YAML subset parser (mappings, sequences, block scalars, flow forms)
# --------------------------------------------------------------------------- #
def _strip_comment(line: str) -> str:
    out, quote, i = [], None, 0
    while i < len(line):
        c = line[i]
        if quote:
            if c == quote:
                if quote == "'" and i + 1 < len(line) and line[i + 1] == "'":
                    out.append("''")
                    i += 2
                    continue
                quote = None
            out.append(c)
        elif c in "'\"":
            quote = c
            out.append(c)
        elif c == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        else:
            out.append(c)
        i += 1
    return "".join(out)


def _split_kv(text: str):
    quote = None
    depth = 0
    i = 0
    while i < len(text):
        c = text[i]
        if quote:
            if c == quote:
                quote = None
        elif c in "'\"":
            quote = c
        elif c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
        elif c == ":" and depth == 0 and (i + 1 == len(text) or text[i + 1] == " "):
            return text[:i].strip(), text[i + 1:].strip()
        i += 1
    return None


def _split_top(text: str, sep: str = ","):
    parts, buf, quote, depth = [], [], None, 0
    for c in text:
        if quote:
            buf.append(c)
            if c == quote:
                quote = None
        elif c in "'\"":
            quote = c
            buf.append(c)
        elif c in "[{":
            depth += 1
            buf.append(c)
        elif c in "]}":
            depth -= 1
            buf.append(c)
        elif c == sep and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(c)
    if "".join(buf).strip():
        parts.append("".join(buf).strip())
    return parts


def _scalar(text: str):
    t = text.strip()
    if len(t) >= 2 and t[0] == "'" and t[-1] == "'":
        return t[1:-1].replace("''", "'")
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
        return t[1:-1].replace('\\"', '"').replace("\\n", "\n")
    if t.startswith("[") and t.endswith("]"):
        return [_scalar(x) for x in _split_top(t[1:-1])]
    if t.startswith("{") and t.endswith("}"):
        out = {}
        for part in _split_top(t[1:-1]):
            kv = _split_kv(part)
            if kv:
                out[kv[0]] = _scalar(kv[1])
        return out
    if t in ("true", "True"):
        return True
    if t in ("false", "False"):
        return False
    if t in ("null", "~", ""):
        return None
    if re.fullmatch(r"-?\d+", t):
        return int(t)
    if re.fullmatch(r"-?\d+\.\d+", t):
        return float(t)
    return t


class _Parser:
    def __init__(self, text: str):
        self.rows = []
        for lineno, raw in enumerate(text.splitlines(), 1):
            s = _strip_comment(raw).rstrip()
            if not s.strip() or s.strip() in ("---", "..."):
                continue
            self.rows.append([len(s) - len(s.lstrip(" ")), s.strip(), lineno])

    def parse(self):
        if not self.rows:
            return {}, {}
        value, _ = self._block(0, self.rows[0][0])
        return value, {}

    def _block(self, i, indent):
        if i >= len(self.rows):
            return None, i
        if self.rows[i][1] == "-" or self.rows[i][1].startswith("- "):
            return self._seq(i, indent)
        return self._map(i, indent)

    def _seq(self, i, indent):
        out = []
        while i < len(self.rows) and self.rows[i][0] == indent and (
            self.rows[i][1] == "-" or self.rows[i][1].startswith("- ")
        ):
            rest = self.rows[i][1][1:].strip()
            if not rest:
                i += 1
                if i < len(self.rows) and self.rows[i][0] > indent:
                    val, i = self._block(i, self.rows[i][0])
                else:
                    val = None
                out.append(val)
            elif _split_kv(rest):
                self.rows[i] = [indent + 2, rest, self.rows[i][2]]
                val, i = self._map(i, indent + 2)
                out.append(val)
            else:
                out.append(_scalar(rest))
                i += 1
        return out, i

    def _map(self, i, indent):
        out = {}
        while i < len(self.rows) and self.rows[i][0] == indent:
            if self.rows[i][1] == "-" or self.rows[i][1].startswith("- "):
                break
            kv = _split_kv(self.rows[i][1])
            if not kv:
                i += 1
                continue
            key, rest = kv
            line = self.rows[i][2]
            if rest in ("|", "|-", "|+", ">", ">-", ">+"):
                i += 1
                val, i = self._block_scalar(i, indent)
            elif rest == "":
                i += 1
                if i < len(self.rows) and self.rows[i][0] > indent:
                    val, i = self._block(i, self.rows[i][0])
                elif i < len(self.rows) and self.rows[i][0] == indent and (
                    self.rows[i][1] == "-" or self.rows[i][1].startswith("- ")
                ):
                    val, i = self._seq(i, indent)
                else:
                    val = None
            else:
                val = _scalar(rest)
                i += 1
            out[key] = val
            out.setdefault("__lines__", {})[key] = line
        return out, i

    def _block_scalar(self, i, parent_indent):
        if i >= len(self.rows) or self.rows[i][0] <= parent_indent:
            return "", i
        base = self.rows[i][0]
        parts = []
        while i < len(self.rows) and self.rows[i][0] >= base:
            parts.append(" " * (self.rows[i][0] - base) + self.rows[i][1])
            i += 1
        return "\n".join(parts), i


def parse_yaml(text: str):
    return _Parser(text).parse()[0] or {}


# --------------------------------------------------------------------------- #
# Expression evaluation (tri-state)
# --------------------------------------------------------------------------- #
_TOKEN = re.compile(
    r"""\s*(?:(\|\||&&|==|!=|<=|>=|!|<|>|\(|\)|,|\[|\]|\.|\*)|'((?:[^']|'')*)'"""
    r"""|"((?:[^"\\]|\\.)*)"|(-?\d+(?:\.\d+)?)|([A-Za-z_][A-Za-z0-9_\-]*))"""
)
STATUS_FNS = ("success(", "failure(", "always(", "cancelled(")


def _truth(v):
    if v is UNKNOWN:
        return UNKNOWN
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    if isinstance(v, (int, float)):
        return v != 0
    return v not in ("", "false", "0")


def _num(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


class Expr:
    def __init__(self, text):
        self.text = text
        self.toks = []
        pos = 0
        while pos < len(text):
            m = _TOKEN.match(text, pos)
            if not m:
                if text[pos:].strip() == "":
                    break
                raise ValueError("bad expression near %r" % text[pos:])
            pos = m.end()
            if m.group(1):
                self.toks.append(("op", m.group(1)))
            elif m.group(2) is not None:
                self.toks.append(("str", m.group(2).replace("''", "'")))
            elif m.group(3) is not None:
                self.toks.append(("str", m.group(3).replace('\\"', '"')))
            elif m.group(4) is not None:
                g = m.group(4)
                self.toks.append(("num", float(g) if "." in g else int(g)))
            else:
                self.toks.append(("id", m.group(5)))
        self.i = 0

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def take(self):
        tok = self.peek()
        self.i += 1
        return tok

    def expect(self, value):
        kind, val = self.take()
        if val != value:
            raise ValueError("expected %r, got %r" % (value, val))

    def evaluate(self, ctx):
        return self._or(ctx)

    def _or(self, ctx):
        left = self._and(ctx)
        while self.peek() == ("op", "||"):
            self.take()
            right = self._and(ctx)
            lt, rt = _truth(left), _truth(right)
            if lt is True or rt is True:
                left = True
            elif lt is False and rt is False:
                left = False
            else:
                left = UNKNOWN
        return left

    def _and(self, ctx):
        left = self._cmp(ctx)
        while self.peek() == ("op", "&&"):
            self.take()
            right = self._cmp(ctx)
            lt, rt = _truth(left), _truth(right)
            if lt is False or rt is False:
                left = False
            elif lt is True and rt is True:
                left = True
            else:
                left = UNKNOWN
        return left

    def _cmp(self, ctx):
        left = self._unary(ctx)
        kind, op = self.peek()
        if kind == "op" and op in ("==", "!=", "<", ">", "<=", ">="):
            self.take()
            right = self._unary(ctx)
            if left is UNKNOWN or right is UNKNOWN:
                return UNKNOWN
            ln, rn = _num(left), _num(right)
            a, b = (ln, rn) if (ln is not None and rn is not None) else (left, right)
            try:
                if op == "==":
                    return a == b
                if op == "!=":
                    return a != b
                if op == "<":
                    return a < b
                if op == ">":
                    return a > b
                if op == "<=":
                    return a <= b
                return a >= b
            except TypeError:
                return UNKNOWN
        return left

    def _unary(self, ctx):
        if self.peek() == ("op", "!"):
            self.take()
            v = _truth(self._unary(ctx))
            return UNKNOWN if v is UNKNOWN else (not v)
        return self._primary(ctx)

    def _primary(self, ctx):
        kind, val = self.take()
        if kind == "str":
            return val
        if kind == "num":
            return val
        if kind == "op" and val == "(":
            v = self._or(ctx)
            self.expect(")")
            return v
        if kind != "id":
            raise ValueError("unexpected token %r" % (val,))
        low = val.lower()
        if low == "true":
            return True
        if low == "false":
            return False
        if low == "null":
            return None
        if self.peek() == ("op", "("):
            self.take()
            args = []
            if self.peek() != ("op", ")"):
                args.append(self._or(ctx))
                while self.peek() == ("op", ","):
                    self.take()
                    args.append(self._or(ctx))
            self.expect(")")
            return _call(low, args, ctx)
        node = ctx.get(val, UNKNOWN)
        while True:
            if self.peek()[0] == "op" and self.peek()[1] == ".":
                self.take()
                k = self.take()
                if k[0] != "id":
                    raise ValueError("bad property access")
                node = _prop(node, k[1])
            elif self.peek()[0] == "op" and self.peek()[1] == "[":
                self.take()
                key = self._or(ctx)
                self.expect("]")
                node = _prop(node, key)
            else:
                break
        return node


def _prop(node, key):
    if node is UNKNOWN:
        return UNKNOWN
    if isinstance(node, dict):
        if key in node:
            return node[key]
        if isinstance(node, _UnknownDict):
            return UNKNOWN
        return ""
    if isinstance(node, list):
        try:
            return node[int(key)]
        except (ValueError, TypeError, IndexError):
            return ""
    return UNKNOWN


def _call(name, args, ctx):
    if name in ("success", "failure", "cancelled", "always"):
        if name == "always":
            return True
        status = ctx.get("__status__", UNKNOWN)
        if status is UNKNOWN:
            return UNKNOWN
        return status == {"success": "success", "failure": "failure", "cancelled": "cancelled"}[name]
    if any(a is UNKNOWN for a in args):
        return UNKNOWN
    if name == "contains":
        if len(args) != 2:
            return UNKNOWN
        hay, needle = args
        if isinstance(hay, list):
            return any(str(x) == str(needle) for x in hay)
        return str(needle) in str(hay)
    if name == "startswith":
        return str(args[0]).startswith(str(args[1])) if len(args) == 2 else UNKNOWN
    if name == "endswith":
        return str(args[0]).endswith(str(args[1])) if len(args) == 2 else UNKNOWN
    if name == "format":
        fmt = str(args[0]).replace("{", "%").replace("}", "")
        try:
            return fmt % tuple(args[1:])
        except (TypeError, ValueError):
            return UNKNOWN
    if name == "join":
        return str(args[1]).join(str(x) for x in (args[0] if isinstance(args[0], list) else [args[0]]))
    if name == "tojson":
        return json.dumps(args[0])
    return UNKNOWN  # hashFiles / fromJSON / anything stateful


def eval_if(text, ctx):
    """Evaluate an `if:` string. Returns True / False / UNKNOWN."""
    if text is None:
        return UNKNOWN
    raw = str(text).strip()
    m = re.fullmatch(r"\$\{\{(.*)\}\}", raw, re.S)
    body = m.group(1).strip() if m else raw
    if body == "":
        return UNKNOWN
    try:
        return Expr(body).evaluate(ctx)
    except ValueError:
        return UNKNOWN


# --------------------------------------------------------------------------- #
# Workflow model
# --------------------------------------------------------------------------- #
TRIGGERS = ("push", "pull_request", "workflow_dispatch", "schedule", "release",
            "workflow_call", "workflow_run", "issues", "pull_request_target")


class Job:
    def __init__(self, jid, spec):
        self.id = jid
        self.spec = spec or {}
        self.line = (spec.get("__lines__") or {}).get("__self__", 0) if spec else 0
        needs = self.spec.get("needs")
        self.needs = [needs] if isinstance(needs, str) else list(needs or [])
        self.if_text = self.spec.get("if")
        self.if_line = (self.spec.get("__lines__") or {}).get("if", 0)
        strategy = self.spec.get("strategy") or {}
        matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
        self.matrix = matrix if isinstance(matrix, dict) else None
        self.steps = [s for s in (self.spec.get("steps") or []) if isinstance(s, dict)]
        self.outputs = list((self.spec.get("outputs") or {}).keys()) if isinstance(
            self.spec.get("outputs"), dict) else []


class Model:
    def __init__(self, doc, path="<memory>"):
        self.doc = doc
        self.path = path
        self.triggers = self._triggers(doc.get("on"))
        self.jobs = {}
        for jid, spec in (doc.get("jobs") or {}).items():
            if jid == "__lines__":
                continue
            if not isinstance(spec, dict):
                continue
            job = Job(jid, spec)
            job.line = (spec.get("__lines__") or {}).get("__self__", 0)
            self.jobs[jid] = job
        self.order = self._topo()

    @staticmethod
    def _triggers(on):
        if on is None:
            return []
        if isinstance(on, str):
            return [on]
        if isinstance(on, list):
            return [str(x) for x in on]
        if isinstance(on, dict):
            return [k for k in on if k != "__lines__"]
        return []

    def _topo(self):
        order, seen, stack = [], set(), set()

        def visit(j, chain):
            if j in seen or j in stack:
                return
            stack.add(j)
            for n in self.jobs.get(j, Job(j, {})).needs:
                if n in self.jobs:
                    visit(n, chain + [j])
            stack.discard(j)
            seen.add(j)
            order.append(j)

        for j in sorted(self.jobs):
            visit(j, [])
        return order

    # -- matrix ---------------------------------------------------------- #
    def legs(self, job):
        if not job.matrix:
            return [None]
        keys = [k for k in job.matrix if k not in ("include", "exclude", "__lines__")]
        grid = [{}]
        for k in keys:
            vals = job.matrix.get(k)
            vals = vals if isinstance(vals, list) else [vals]
            grid = [dict(c, **{k: v}) for c in grid for v in vals]
        exclude = job.matrix.get("exclude") or []
        for ex in exclude:
            items = [(k, v) for k, v in ex.items() if not k.startswith("__")] if isinstance(ex, dict) else []
            grid = [c for c in grid if not (items and all(c.get(k) == v for k, v in items))]
        for inc in job.matrix.get("include") or []:
            if not isinstance(inc, dict):
                continue
            matched = False
            for c in grid:
                if all(c.get(k) == v for k, v in inc.items() if k in c and not k.startswith("__")):
                    c.update(inc)
                    matched = True
            if not matched:
                grid.append(dict(inc))
        return grid

    # -- contexts -------------------------------------------------------- #
    def _ctx(self, trigger, needs_state, matrix=None, status="success"):
        needs = {}
        for jid in self.jobs:
            state = needs_state.get(jid)
            if state is None:
                needs[jid] = {"result": UNKNOWN, "outputs": _UnknownDict()}
            else:
                res, outs = state
                needs[jid] = {"result": res, "outputs": dict(outs) if outs is not None else _UnknownDict()}
        return {
            "github": {"event_name": trigger, "ref": "refs/heads/main",
                       "event": _UnknownDict(), "repository": "owner/repo"},
            "needs": needs,
            "matrix": dict(matrix or {}),
            "env": {},
            "secrets": _UnknownDict(),
            "runner": {"os": "Linux"},
            "job": {"status": status},
            "__status__": status,
        }

    def runs_under(self, job, trigger, needs_state, matrix, status="success"):
        """True/False/UNKNOWN: does the job run (before its own failure is known)?"""
        ctx = self._ctx(trigger, needs_state, matrix, status)
        if job.if_text is None:
            for n in job.needs:
                st = needs_state.get(n)
                if st is None or st[0] == UNKNOWN:
                    return UNKNOWN
                if st[0] != "success":
                    return False
            return True
        val = eval_if(job.if_text, ctx)
        if val is UNKNOWN:
            return UNKNOWN
        if not _truth(val):
            return False
        if not any(fn in str(job.if_text) for fn in STATUS_FNS):
            for n in job.needs:
                st = needs_state.get(n)
                if st is None or st[0] is UNKNOWN:
                    return UNKNOWN
                if st[0] != "success":
                    return False
        return True

    # -- possibility analysis -------------------------------------------- #
    def possibilities(self):
        """For each job: can_run / must_run / can_fail / can_skip over all
        reachable need-outcome combinations and all declared triggers."""
        out = {}
        combos_cache = {}

        def combos(job):
            if job.id in combos_cache:
                return combos_cache[job.id]
            needs = [n for n in job.needs if n in self.jobs]
            acc = [{}]
            for n in needs:
                p = out.get(n, {"can_skip": True})
                options = ["success"]
                if p.get("can_fail"):
                    options.append("failure")
                if p.get("can_skip"):
                    options.append("skipped")
                acc = [dict(c, **{n: (o, None)}) for c in acc for o in options]
            combos_cache[job.id] = acc
            return acc

        for jid in self.order:
            job = self.jobs[jid]
            triggers = self.triggers or ["push"]
            can_run = False
            determinate = True
            must_run = bool(triggers)
            for trig in triggers:
                for state in combos(job):
                    verdicts = set()
                    legs = self.legs(job)
                    for leg in legs:
                        verdicts.add(self.runs_under(job, trig, state, leg))
                    if True in verdicts or UNKNOWN in verdicts:
                        can_run = True
                    else:
                        must_run = False
                    if UNKNOWN in verdicts:
                        determinate = False
                        must_run = False
            if not job.needs:
                must_run = bool(triggers) and self.runs_under(job, triggers[0], {}, (self.legs(job) or [None])[0]) is True \
                    if triggers else False
                if len(triggers) > 1:
                    must_run = all(self.runs_under(job, t, {}, (self.legs(job) or [None])[0]) is True for t in triggers)
            out[jid] = {"can_run": can_run, "can_fail": can_run, "must_run": must_run,
                        "can_skip": not must_run, "determinate": determinate,
                        "legs": len(self.legs(job))}
        return out


class _UnknownDict(dict):
    """dict whose missing keys are UNKNOWN instead of empty string."""

    def __missing__(self, key):
        return UNKNOWN

    def get(self, key, default=None):
        return dict.get(self, key, UNKNOWN)


# --------------------------------------------------------------------------- #
# Scenario simulation + 1-minimal witness
# --------------------------------------------------------------------------- #
def simulate(model, trigger, known):
    """Run the DAG with a set of *observed facts*.

    Facts look like: 'trigger=push', 'result:<job>=failure',
    'output:<job>.<key>=true'. Anything not observed is UNKNOWN, so a verdict
    is only produced when the observations logically force it.
    """
    results, outs = {}, {}
    for fact in known:
        if fact.startswith("result:"):
            jid, _, val = fact[7:].partition("=")
            results[jid] = val
        elif fact.startswith("output:"):
            rest, _, val = fact[7:].partition("=")
            jid, _, key = rest.partition(".")
            outs.setdefault(jid, {})[key] = val
    state = {}
    for jid in model.order:
        job = model.jobs[jid]
        need_state = {n: (results.get(n, UNKNOWN), outs.get(n)) for n in job.needs}
        verdicts = set()
        for leg in model.legs(job):
            verdicts.add(model.runs_under(job, trigger, need_state, leg))
        if "trigger=" + str(trigger) not in known:
            v = UNKNOWN
        elif verdicts == {True}:
            r = results.get(jid, UNKNOWN)
            v = r if r is not UNKNOWN else "runs"
        elif verdicts == {False}:
            v = "skipped"
        else:
            v = UNKNOWN
        if v is not UNKNOWN:
            state[jid] = v
    return state


def minimize(facts, target, verdict, model, trigger):
    """Greedy 1-minimal witness: drop any fact that is not needed to force
    the same verdict for `target`."""
    witness = sorted(set(facts), key=lambda f: (f.startswith("trigger="), f))

    def holds(w):
        return simulate(model, trigger, w).get(target, UNKNOWN) == verdict

    if not holds(witness):
        return None
    for fact in list(witness):
        trial = [f for f in witness if f != fact]
        if trial and holds(trial):
            witness = trial
    return witness


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #
def _has_contradiction(text):
    body = re.sub(r"^\s*\$\{\{|\}\}\s*$", "", str(text or "").strip())
    parts = [re.sub(r"[\s()]", "", p) for p in re.split(r"&&", body)]
    return any("!" + p in parts for p in parts if p and not p.startswith("!"))


def run_rules(model):
    findings = []
    poss = model.possibilities()
    ids = set(model.jobs)

    def add(rule, job, msg, line=0, severity="error"):
        findings.append({"rule": rule, "job": job, "severity": severity,
                         "message": msg, "line": line or 0})

    if not model.jobs:
        add("AR014", "-", "workflow declares no jobs; nothing can ever run")
    if not model.triggers:
        add("AR015", "-", "no `on:` triggers declared; the workflow can never start")

    for jid in model.order:
        job = model.jobs[jid]
        p = poss[jid]
        line = job.line
        # AR002 unknown needs target
        for n in job.needs:
            if n not in ids:
                add("AR002", jid, "needs unknown job %r; GitHub rejects this, the job never runs" % n, line)
        # AR006 needs.X referenced but not declared in needs
        refs = set(re.findall(r"needs\.([A-Za-z0-9_\-]+)", str(job.if_text or "")))
        for r in refs:
            if r not in job.needs and r != "__lines__":
                add("AR006", jid, "if references needs.%s but the job does not list it in `needs:`; "
                                  "the value is empty, so any == comparison is permanently false" % r,
                    job.if_line)
        # AR004 output not declared by the needed job
        for m in re.finditer(r"needs\.([A-Za-z0-9_\-]+)\.outputs\.([A-Za-z0-9_\-]+)", str(job.if_text or "")):
            src, key = m.group(1), m.group(2)
            dep = model.jobs.get(src)
            if dep is not None and key not in dep.outputs:
                add("AR004", jid, "if reads needs.%s.outputs.%s, but %s declares no such output; "
                                  "the value is always empty" % (src, key, src), job.if_line)
        # AR005 contradiction
        if _has_contradiction(job.if_text):
            add("AR005", jid, "if contains a contradiction (X && !X); the job can never run", job.if_line)
        # AR001 dead by constant false
        if job.if_text is not None and not p["can_run"] and p["determinate"] and all(n in ids for n in job.needs):
            if not any(poss[n]["can_run"] is False for n in job.needs if n in ids):
                add("AR001", jid, "if is false under every trigger; the job is dead code", job.if_line)
        # AR009 unreachable because a need can never run
        for n in job.needs:
            if n in poss and poss[n]["can_run"] is False:
                add("AR009", jid, "needs %s, which can never run; the whole chain is dead" % n, line)
        # AR003 cycle
        def reaches(start, target, seen=None):
            seen = seen or set()
            for n in model.jobs[start].needs:
                if n == target:
                    return True
                if n in model.jobs and n not in seen:
                    seen.add(n)
                    if reaches(n, target, seen):
                        return True
            return False

        for n in job.needs:
            if n in model.jobs and reaches(n, jid):
                add("AR003", jid, "needs cycle through %s; no job in the cycle can ever run" % n, line)
                break
        # AR007 empty matrix
        if job.matrix and not model.legs(job):
            add("AR007", jid, "matrix expands to zero legs after exclude; the job never runs", line)
        # AR008 matrix key not defined
        if job.matrix:
            declared = set(k for k in job.matrix if k not in ("include", "exclude", "__lines__"))
            for inc in job.matrix.get("include") or []:
                if isinstance(inc, dict):
                    declared |= set(inc.keys())
            body = json.dumps([job.spec], default=str)
            for m in re.finditer(r"matrix\.([A-Za-z0-9_]+)", body):
                if m.group(1) not in declared:
                    add("AR008", jid, "matrix.%s is used but not declared; legs silently lose it" % m.group(1), line)
                    break
        # AR012 trigger never enabled
        for m in re.finditer(r"github\.event_name\s*==\s*'([^']+)'", str(job.if_text or "")):
            if model.triggers and m.group(1) not in model.triggers:
                add("AR012", jid, "if requires event_name == %r but `on:` never triggers it" % m.group(1), job.if_line)
        # AR010 always() with needs that can fail
        if "always(" in str(job.if_text or "") and any(
                poss.get(n, {}).get("can_fail") for n in job.needs):
            add("AR010", jid, "if uses always() with needs that can fail; it will run even when its "
                              "dependency failed (artifacts may be missing)", job.if_line, "warning")
        # AR011 silent skip propagation
        if job.if_text is None and any(poss.get(n, {}).get("can_skip") for n in job.needs):
            add("AR011", jid, "no `if:` and a need can be skipped; when it is, this job is silently "
                              "skipped too (no annotation says why)", line, "warning")
        # AR013 step gated on an impossible matrix value
        for step in job.steps:
            st_if = str(step.get("if") or "")
            if not st_if or not job.matrix:
                continue
            if re.search(r"\bfalse\b", st_if) and "matrix." not in st_if:
                add("AR013", jid, "step %r has an if that is always false" % step.get("name", "?"), 0, "warning")
            for m in re.finditer(r"matrix\.([A-Za-z0-9_]+)\s*==\s*'([^']*)'", st_if):
                vals = [leg.get(m.group(1)) for leg in model.legs(job) if leg]
                if vals and m.group(2) not in [str(v) for v in vals]:
                    add("AR013", jid, "step %r requires matrix.%s == %r, but no leg has that value"
                        % (step.get("name", "?"), m.group(1), m.group(2)), 0, "warning")
    return findings


# --------------------------------------------------------------------------- #
# Rule fixtures (1-minimal counterexamples): the clean and defect files are
# required to differ by exactly ONE line, so no single line can be removed
# from the counterexample without turning it into the passing case.
# --------------------------------------------------------------------------- #
FIXTURES = {
    "AR001": (
        """on: push
jobs:
  a:
    runs-on: ubuntu-latest
    steps: [{run: echo hi}]
""",
        """on: push
jobs:
  a:
    runs-on: ubuntu-latest
    if: ${{ false }}
    steps: [{run: echo hi}]
"""),
    "AR002": (
        """on: push
jobs:
  a:
    runs-on: ubuntu-latest
    steps: [{run: echo hi}]
""",
        """on: push
jobs:
  a:
    needs: nope
    runs-on: ubuntu-latest
    steps: [{run: echo hi}]
"""),
    "AR003": (
        """on: push
jobs:
  a:
    needs: b
    runs-on: ubuntu-latest
    steps: [{run: echo hi}]
  b:
    runs-on: ubuntu-latest
    steps: [{run: echo hi}]
""",
        """on: push
jobs:
  a:
    needs: b
    runs-on: ubuntu-latest
    steps: [{run: echo hi}]
  b:
    needs: a
    runs-on: ubuntu-latest
    steps: [{run: echo hi}]
"""),
    "AR004": (
        """on: push
jobs:
  check:
    runs-on: ubuntu-latest
    outputs:
      ready: ${{ steps.s.outputs.ready }}
    steps: [{run: echo hi}]
  build:
    needs: check
    if: needs.check.outputs.ready == 'true'
    runs-on: ubuntu-latest
    steps: [{run: echo hi}]
""",
        """on: push
jobs:
  check:
    runs-on: ubuntu-latest
    outputs:
      ready: ${{ steps.s.outputs.ready }}
    steps: [{run: echo hi}]
  build:
    needs: check
    if: needs.check.outputs.redy == 'true'
    runs-on: ubuntu-latest
    steps: [{run: echo hi}]
"""),
    "AR005": (
        """on: push
jobs:
  a:
    runs-on: ubuntu-latest
    if: ${{ github.event_name == 'push' }}
    steps: [{run: echo hi}]
""",
        """on: push
jobs:
  a:
    runs-on: ubuntu-latest
    if: ${{ github.event_name == 'push' && !(github.event_name == 'push') }}
    steps: [{run: echo hi}]
"""),
    "AR006": (
        """on: push
jobs:
  check:
    runs-on: ubuntu-latest
    outputs:
      ready: ${{ steps.s.outputs.ready }}
    steps: [{run: echo hi}]
  build:
    needs: check
    if: needs.check.outputs.ready == 'true'
    runs-on: ubuntu-latest
    steps: [{run: echo hi}]
""",
        """on: push
jobs:
  check:
    runs-on: ubuntu-latest
    outputs:
      ready: ${{ steps.s.outputs.ready }}
    steps: [{run: echo hi}]
  build:
    if: needs.check.outputs.ready == 'true'
    runs-on: ubuntu-latest
    steps: [{run: echo hi}]
"""),
    "AR007": (
        """on: push
jobs:
  a:
    strategy:
      matrix:
        os: [linux, mac]
        exclude:
          - os: mac
    runs-on: ubuntu-latest
    steps: [{run: echo hi}]
""",
        """on: push
jobs:
  a:
    strategy:
      matrix:
        os: [linux, mac]
        exclude:
          - os: linux
          - os: mac
    runs-on: ubuntu-latest
    steps: [{run: echo hi}]
"""),
    "AR008": (
        """on: push
jobs:
  a:
    strategy:
      matrix:
        os: [linux, mac]
    runs-on: ubuntu-latest
    steps: [{run: echo ${{ matrix.os }}}]
""",
        """on: push
jobs:
  a:
    strategy:
      matrix:
        os: [linux, mac]
    runs-on: ubuntu-latest
    steps: [{run: echo ${{ matrix.arch }}}]
"""),
    "AR009": (
        """on: push
jobs:
  a:
    runs-on: ubuntu-latest
    if: ${{ false }}
    steps: [{run: echo hi}]
  b:
    runs-on: ubuntu-latest
    steps: [{run: echo hi}]
""",
        """on: push
jobs:
  a:
    runs-on: ubuntu-latest
    if: ${{ false }}
    steps: [{run: echo hi}]
  b:
    needs: a
    runs-on: ubuntu-latest
    steps: [{run: echo hi}]
"""),
    "AR010": (
        """on: push
jobs:
  a:
    runs-on: ubuntu-latest
    steps: [{run: echo hi}]
  b:
    needs: a
    if: success()
    runs-on: ubuntu-latest
    steps: [{run: echo hi}]
""",
        """on: push
jobs:
  a:
    runs-on: ubuntu-latest
    steps: [{run: echo hi}]
  b:
    needs: a
    if: always()
    runs-on: ubuntu-latest
    steps: [{run: echo hi}]
"""),
    "AR011": (
        """on: push
jobs:
  a:
    runs-on: ubuntu-latest
    if: github.event_name == 'push'
    steps: [{run: echo hi}]
  b:
    needs: a
    runs-on: ubuntu-latest
    steps: [{run: echo hi}]
""",
        """on: push
jobs:
  a:
    runs-on: ubuntu-latest
    if: github.event_name == 'release'
    steps: [{run: echo hi}]
  b:
    needs: a
    runs-on: ubuntu-latest
    steps: [{run: echo hi}]
"""),
    "AR012": (
        """on: push
jobs:
  a:
    runs-on: ubuntu-latest
    if: github.event_name == 'push'
    steps: [{run: echo hi}]
""",
        """on: push
jobs:
  a:
    runs-on: ubuntu-latest
    if: github.event_name == 'release'
    steps: [{run: echo hi}]
"""),
    "AR013": (
        """on: push
jobs:
  a:
    strategy:
      matrix:
        os: [linux, mac]
    runs-on: ubuntu-latest
    steps:
      - name: s
        if: matrix.os == 'mac'
        run: echo hi
""",
        """on: push
jobs:
  a:
    strategy:
      matrix:
        os: [linux, mac]
    runs-on: ubuntu-latest
    steps:
      - name: s
        if: matrix.os == 'windows'
        run: echo hi
"""),
    "AR014": (
        """on: push
jobs:
  a:
    runs-on: ubuntu-latest
    steps: [{run: echo hi}]
""",
        """on: push
jobs: {}
"""),
    "AR015": (
        """on: push
jobs:
  a:
    runs-on: ubuntu-latest
    steps: [{run: echo hi}]
""",
        """jobs:
  a:
    runs-on: ubuntu-latest
    steps: [{run: echo hi}]
"""),
}


def self_test(verbose=True):
    results = []
    for rule, (clean, bad) in sorted(FIXTURES.items()):
        import difflib
        sm = difflib.SequenceMatcher(None, clean.splitlines(), bad.splitlines())
        diff = sum(1 for op in sm.get_opcodes() if op[0] != "equal")
        clean_rules = {f["rule"] for f in run_rules(Model(parse_yaml(clean)))}
        bad_rules = {f["rule"] for f in run_rules(Model(parse_yaml(bad)))}
        ok = (diff == 1 and rule in bad_rules and rule not in clean_rules)
        results.append({"rule": rule, "diff_lines": diff, "fires_on_defect": rule in bad_rules,
                        "silent_on_clean": rule not in clean_rules, "ok": ok})
        if verbose:
            print("%s %-7s diff=%d fires=%s silent=%s" % (
                "PASS" if ok else "FAIL", rule, diff, rule in bad_rules, rule not in clean_rules))
    return results


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def analyse(paths, trigger=None, known=None, want_json=False):
    known = list(known or [])
    findings, jobs, models = [], {}, []
    for path in paths:
        try:
            text = open(path, encoding="utf-8").read()
        except OSError as exc:
            raise SystemExit(_usage_error("cannot read %s: %s" % (path, exc)))
        doc = parse_yaml(text)
        model = Model(doc, path)
        models.append((path, model))
        for f in run_rules(model):
            f["file"] = path
            findings.append(f)
        if trigger:
            state = simulate(model, trigger, known + ["trigger=" + trigger])
            for jid in model.order:
                verdict = state.get(jid, "unknown")
                entry = {"file": path, "job": jid, "verdict": verdict,
                         "needs": model.jobs[jid].needs,
                         "legs": len(model.legs(model.jobs[jid]))}
                if verdict not in ("unknown",):
                    facts = known + ["trigger=" + trigger]
                    facts += ["result:%s=%s" % (j, v) for j, v in state.items()]
                    for jid2, job2 in model.jobs.items():
                        for key in job2.outputs:
                            val = dict(x.partition("=")[::2] for x in known).get("output:%s.%s" % (jid2, key))
                            if val is not None:
                                facts.append("output:%s.%s=%s" % (jid2, key, val))
                    wit = minimize(facts, jid, verdict, model, trigger)
                    entry["witness"] = wit
                    entry["witness_size"] = len(wit) if wit else 0
                    entry["witness_full"] = len(set(facts))
                jobs.setdefault(path, []).append(entry)
    report = {"tool": "actions-reach", "version": "1.0.0",
              "files": paths, "triggers": {p: m.triggers for p, m in models},
              "findings": findings,
              "summary": {"files": len(paths), "jobs": sum(len(m.jobs) for _, m in models),
                          "errors": sum(1 for f in findings if f["severity"] == "error"),
                          "warnings": sum(1 for f in findings if f["severity"] == "warning")}}
    if trigger:
        report["scenario"] = {"trigger": trigger, "known": known, "jobs": jobs}
    return report


def _usage_error(msg):
    print("actions-reach: " + msg, file=sys.stderr)
    return 2


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0 if argv else 2
    if argv[0] == "--self-test":
        res = self_test(verbose="--json" not in argv)
        if "--json" in argv:
            print(json.dumps({"self_test": res, "passed": sum(1 for r in res if r["ok"]),
                              "total": len(res)}, indent=2))
        proven = all(r["ok"] for r in res)
        if "--json" not in argv:
            print("\nself-test: %d/%d proven" % (sum(1 for r in res if r["ok"]), len(res)))
        return 0 if proven else 2
    want_json = "--json" in argv
    trigger, known, paths = None, [], []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--json":
            i += 1
        elif a == "--trigger":
            if i + 1 >= len(argv):
                return _usage_error("--trigger needs a value")
            trigger = argv[i + 1]
            i += 2
        elif a == "--assume":
            if i + 1 >= len(argv):
                return _usage_error("--assume needs a value")
            for item in argv[i + 1].split(","):
                if item:
                    known.append("result:" + item if "=" in item else "result:%s=success" % item)
            i += 2
        elif a == "--output":
            if i + 1 >= len(argv) or "=" not in argv[i + 1]:
                return _usage_error("--output needs job.key=value")
            rest, _, val = argv[i + 1].partition("=")
            jid, _, key = rest.partition(".")
            if not jid or not key:
                return _usage_error("--output needs job.key=value")
            known.append("output:%s.%s=%s" % (jid, key, val))
            i += 2
        elif a.startswith("-"):
            return _usage_error("unknown option %s" % a)
        else:
            paths.append(a)
            i += 1
    if not paths:
        return _usage_error("no workflow files given")
    report = analyse(paths, trigger, known, want_json)
    if want_json:
        print(json.dumps(report, indent=2, sort_keys=False))
    else:
        for f in report["findings"]:
            print("%-6s %-7s %-18s %s" % (f["severity"][:6], f["rule"], f["job"], f["message"]))
        if trigger:
            print("\nscenario trigger=%s" % trigger)
            for path, entries in report["scenario"]["jobs"].items():
                for e in entries:
                    print("  %-20s -> %-8s legs=%d witness=%s/%s %s" % (
                        e["job"], e["verdict"], e["legs"], e.get("witness_size", 0),
                        e.get("witness_full", 0), ",".join(e.get("witness") or [])))
        s = report["summary"]
        print("\n%d file(s), %d job(s), %d error(s), %d warning(s)"
              % (s["files"], s["jobs"], s["errors"], s["warnings"]))
    return 1 if report["summary"]["errors"] or (trigger and any(
        e["verdict"] == "unknown" for es in report["scenario"]["jobs"].values() for e in es)) else 0


USAGE = """actions-reach -- offline GitHub Actions job-reachability simulator

  actions-reach [--json] [--trigger EVENT] [--assume job=result,...]
                [--output job.key=value] WORKFLOW.yml [...]
  actions-reach --self-test [--json]

Exit codes: 0 = no error findings, 1 = findings (or undetermined verdicts),
            2 = usage / unreadable input.
"""


if __name__ == "__main__":
    sys.exit(main())
