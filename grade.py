#!/usr/bin/env python3
"""CSCE 3304 project grader.

    python3 grade.py OPTION --sub DIR [--only core|full] [--tests DIR] [--timeout SEC]

OPTION      sasic  (Option 1, structured-ASIC placement and clock tree)
            macro  (Option 2, macro placement in a floorplan)
            fm     (Option 3, FM partitioner)
            maze   (Option 4, maze router)
            cover  (Option 5, tree-covering mapper)
--sub       the student's repository; it must contain run.py (see the handout).
--only      run only the core cases or only the full cases (default: both, the final submission).

The intermediate checkpoint (November 26) runs --only core: the core cases, worth 10.
The final submission (December 5) runs everything: the student's program on every case named
in tests/manifest.json (the core cases, then the full cases).  The grader prints PASS or FAIL
per case and the automatic score: core cases 10, full cases 30, visualization 10.
Cases whose input is not present (hidden files in the student release) are reported as SKIP.
"""
import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- utilities

class Result:
    def __init__(self):
        self.rows = []

    def add(self, case, ok, detail="", item=None):
        self.rows.append((case, ok, detail, item))
        tag = {True: "PASS", False: "FAIL", None: "SKIP"}[ok]
        print(f"{tag}  {case}" + (f"  -- {detail}" if detail else ""))


def lines_of(text):
    out = [l.rstrip() for l in text.replace("\r\n", "\n").split("\n")]
    while out and out[-1] == "":
        out.pop()
    while out and out[0] == "":
        out.pop(0)
    return out


def run_student(sub, args, timeout):
    prog = os.path.join(sub, "run.py")
    if not os.path.isfile(prog):
        return None, f"{prog} not found"
    try:
        p = subprocess.run([sys.executable, prog] + args, capture_output=True, text=True,
                           timeout=timeout, cwd=sub)
    except subprocess.TimeoutExpired:
        return None, f"timeout after {timeout}s"
    if p.returncode != 0:
        err = (p.stderr.strip().splitlines() or ["(no stderr)"])[-1]
        return None, f"exit code {p.returncode}: {err[:200]}"
    return p.stdout, ""


def exact_cmp(out, expected_path):
    exp = lines_of(open(expected_path).read())
    got = lines_of(out)
    if got == exp:
        return True, ""
    for i in range(max(len(got), len(exp))):
        g = got[i] if i < len(got) else "<missing>"
        e = exp[i] if i < len(exp) else "<extra line>"
        if g != e:
            return False, f"line {i + 1}: got '{g}' expected '{e}'"
    return False, "mismatch"


def exact(res, case, out, expected_path, item=None):
    ok, detail = exact_cmp(out, expected_path)
    res.add(case, ok, detail, item)
    return ok


def tokens(path):
    for raw in open(path):
        t = raw.split("#")[0].split()
        if t:
            yield t


# ---------------------------------------------------------------- structured ASIC

def sa_design(fdir, ddir=None):
    """Independent reader for a sasic fabric directory and (optionally) a design directory."""
    d = dict(types={}, tmpl=[], pins={}, pin_dir={}, insts={}, nets=[], clock=None, fab={}, raw_nets=[], sinks=set())
    for t in tokens(os.path.join(fdir, "fabric.txt")):
        if t[0] in ("site", "tile", "tiles", "origin"):
            d[t[0]] = (int(t[1]), int(t[2]))
        elif t[0] == "name":
            d["name"] = t[1]
        elif t[0] == "celltype":
            d["types"][t[1]] = (int(t[2]), t[3])
        elif t[0] == "cell":
            rep, sp = (int(t[5]), int(t[6])) if len(t) > 5 else (1, 0)
            d["tmpl"].append((t[1], t[2], int(t[3]), int(t[4]), rep, sp))
    (sw, rh), (tw, th), (nx, ny), (ox, oy) = d["site"], d["tile"], d["tiles"], d["origin"]
    for ty in range(ny):
        for tx in range(nx):
            for nm, typ, cx, cy, rep, sp in d["tmpl"]:
                w = d["types"][typ][0]
                for i in range(rep):
                    name = f"T{tx}Y{ty}__{nm}" + (f"_{i}" if rep > 1 else "")
                    llx, lly = ox + (tx * tw + cx + i * sp) * sw, oy + (ty * th + cy) * rh
                    d["fab"][name] = (typ, llx + w * sw // 2, lly + rh // 2)
                    d.setdefault("fab_ll", {})[name] = (llx, lly, "N" if cy % 2 == 0 else "FS")
    for t in tokens(os.path.join(fdir, "pins.txt")):
        if t[0] == "pin":
            d["pins"]["pin:" + t[1]] = (int(t[2]), int(t[3]))
            d["pin_dir"][t[1]] = t[4]
        elif t[0] == "die":
            d["die"] = (int(t[1]), int(t[2]))
    if ddir is None:
        return d
    allnets = []
    for t in tokens(os.path.join(ddir, "netlist.txt")):
        if t[0] == "cell":
            d["insts"][t[1]] = t[2]
        elif t[0] == "net":
            allnets.append((t[1], t[2:]))
        elif t[0] == "clock":
            d["clock"] = t[1]
    obj = lambda x: x if x.startswith("pin:") else x.split("/")[0]
    d["raw_nets"] = allnets
    d["nets"] = [list(dict.fromkeys(obj(x) for x in ts)) for n, ts in allnets if n != d["clock"]]
    d["sinks"] = {obj(x) for n, ts in allnets if n == d["clock"] for x in ts if not x.startswith("pin:")}
    d["max_skew"] = d["max_latency"] = None
    cf = os.path.join(ddir, "clock.txt")
    if os.path.isfile(cf):
        for t in tokens(cf):
            if t[0] == "root":
                d["root"] = t[1]
            elif t[0] in ("fanout", "bufdelay"):
                d[t[0]] = int(t[1])
            elif t[0] == "caps" and t[1] == d["name"]:
                d["max_skew"], d["max_latency"] = int(t[2]), int(t[3])
    return d


def sa_check_placement(d, text):
    """Lines 'CELL FABRIC_CELL', every netlist cell once, sorted by cell name.  Returns ({term: (x, y)}, error)."""
    pl, names, used = {}, [], {}
    for i, l in enumerate(lines_of(text)):
        t = l.split()
        if len(t) != 2:
            return None, f"line {i + 1}: expected 'CELL FABRIC_CELL'"
        c, f = t
        if c not in d["insts"]:
            return None, f"unknown netlist cell {c}"
        if c in pl:
            return None, f"{c} placed twice"
        if f not in d["fab"]:
            return None, f"{c}: no fabric cell named {f}"
        ftype = d["fab"][f][0]
        if d["types"][ftype][1] != "logic":
            return None, f"{c}: {f} is a {d['types'][ftype][1]} cell"
        if ftype != d["insts"][c]:
            return None, f"{c} is {d['insts'][c]} but {f} is {ftype}"
        if f in used:
            return None, f"{used[f]} and {c} share {f}"
        used[f] = c
        pl[c] = d["fab"][f][1:]
        names.append(c)
    if names != sorted(names):
        return None, "lines not sorted by cell name"
    missing = sorted(set(d["insts"]) - set(pl))
    if missing:
        return None, f"missing cells {missing[:4]}"
    pl.update(d["pins"])
    return pl, ""


def sa_twl(d, pl):
    tot = 0
    for ms in d["nets"]:
        if len(ms) >= 2:
            pts = [pl[m] for m in ms]
            tot += max(p[0] for p in pts) - min(p[0] for p in pts) + max(p[1] for p in pts) - min(p[1] for p in pts)
    return tot


def sa_check_clock(d, pl, text):
    """Return dict(legal, err, wire, skew, lat)."""
    r = dict(legal=False, err="", wire=None, skew=None, lat=None)
    nodes, edges = {}, []
    for i, l in enumerate(lines_of(text)):
        t = l.split()
        if len(t) == 3 and t[0] == "NODE" and t[2] == "root":
            nodes.setdefault(t[1], []).append(("root", None))
        elif len(t) == 4 and t[0] == "NODE" and t[2] in ("buf", "sink"):
            nodes.setdefault(t[1], []).append((t[2], t[3]))
        elif len(t) == 3 and t[0] == "EDGE":
            edges.append((t[1], t[2]))
        else:
            r["err"] = f"line {i + 1}: unrecognised '{l}'"
            return r
    if any(len(v) > 1 for v in nodes.values()):
        r["err"] = "a node id is repeated"
        return r
    nodes = {k: v[0] for k, v in nodes.items()}
    roots = [n for n, (k, _) in nodes.items() if k == "root"]
    if len(roots) != 1:
        r["err"] = f"{len(roots)} root nodes"
        return r
    root = roots[0]
    kids, parent = {n: [] for n in nodes}, {}
    for p, c in edges:
        if p not in nodes or c not in nodes:
            r["err"] = f"edge {p} -> {c} names an unknown node"
            return r
        if c in parent:
            r["err"] = f"node {c} has two parents"
            return r
        parent[c] = p
        kids[p].append(c)
    if root in parent:
        r["err"] = "the root has a parent"
        return r
    seen, stack = set(), [root]
    while stack:
        n = stack.pop()
        if n in seen:
            r["err"] = "cycle"
            return r
        seen.add(n)
        stack.extend(kids[n])
    if seen != set(nodes):
        r["err"] = "nodes not reachable from the root"
        return r
    pos, bufs_used, sinks = {}, set(), set()
    for n, (k, ref) in nodes.items():
        if k == "root":
            pos[n] = d["pins"]["pin:" + d["root"]]
        elif k == "buf":
            if ref not in d["fab"] or d["types"][d["fab"][ref][0]][1] != "clock":
                r["err"] = f"buffer {n} is not on a clock-buffer fabric cell ({ref})"
                return r
            if ref in bufs_used:
                r["err"] = f"two buffers on {ref}"
                return r
            bufs_used.add(ref)
            if not kids[n]:
                r["err"] = f"buffer {n} drives nothing"
                return r
            pos[n] = d["fab"][ref][1:]
        else:
            if ref not in d["sinks"]:
                r["err"] = f"{ref} is not a flip-flop on the clock net"
                return r
            if ref in sinks:
                r["err"] = f"{ref} appears twice"
                return r
            if kids[n]:
                r["err"] = f"sink {n} has children"
                return r
            sinks.add(ref)
            pos[n] = pl[ref]
        if k in ("root", "buf") and len(kids[n]) > d["fanout"]:
            r["err"] = f"node {n} drives {len(kids[n])} > fanout {d['fanout']}"
            return r
    if sinks != d["sinks"]:
        r["err"] = f"missing sinks {sorted(d['sinks'] - sinks)[:4]}"
        return r
    delay, nbuf, wire = {}, {}, 0
    stack = [(root, 0, 0)]
    while stack:
        n, t, b = stack.pop()
        if nodes[n][0] == "buf":
            t += d["bufdelay"]
            b += 1
        if nodes[n][0] == "sink":
            delay[n], nbuf[n] = t, b
        for c in kids[n]:
            e = abs(pos[n][0] - pos[c][0]) + abs(pos[n][1] - pos[c][1])
            wire += e
            stack.append((c, t + e, b))
    r["wire"] = wire
    if len(set(nbuf.values())) > 1:
        r["err"] = f"sinks see different buffer counts {sorted(set(nbuf.values()))}"
        return r
    r["skew"] = max(delay.values()) - min(delay.values()) if delay else 0
    r["lat"] = max(delay.values()) if delay else 0
    if r["skew"] > d["max_skew"]:
        r["err"] = f"skew {r['skew']} > max_skew {d['max_skew']}"
        return r
    if r["lat"] > d["max_latency"]:
        r["err"] = f"latency {r['lat']} > max_latency {d['max_latency']}"
        return r
    r["legal"] = True
    return r


def sa_parse_def(text):
    """Minimal DEF reader for the sections the handout asks for."""
    body = re.sub(r"#.*", "", text)

    def section(name):
        m = re.search(rf"\b{name}\s+\d+\s*;(.*?)\bEND\s+{name}\b", body, re.S)
        if not m:
            raise ValueError(f"no {name} section")
        return [st.split() for st in m.group(1).split(";") if st.strip()]
    comps, pins, nets = {}, {}, {}
    for t in section("COMPONENTS"):
        if t[0] != "-":
            raise ValueError(f"bad COMPONENTS statement {' '.join(t[:6])}")
        k = next((i for i, x in enumerate(t) if x in ("PLACED", "FIXED")), None)
        if k is None:
            raise ValueError(f"component {t[1]} is not placed")
        if t[1] in comps:
            raise ValueError(f"component {t[1]} appears twice")
        comps[t[1]] = (t[2], int(t[k + 2]), int(t[k + 3]), t[k + 5])
    for t in section("PINS"):
        k = next(i for i, x in enumerate(t) if x in ("PLACED", "FIXED"))
        pins[t[1]] = (t[t.index("NET") + 1], int(t[k + 2]), int(t[k + 3]))
    for t in section("NETS"):
        conns = set(re.findall(r"\(\s*(\S+)\s+(\S+)\s*\)", " ".join(t)))
        if t[1] in nets:
            raise ValueError(f"net {t[1]} appears twice")
        nets[t[1]] = frozenset(conns)
    return comps, pins, nets


def sa_check_def(d, place_text, def_text, clock_text=None):
    """Return (ok, detail).  place_text: the student's placement; clock_text: their clock tree or None."""
    try:
        comps, pins, nets = sa_parse_def(def_text)
    except (ValueError, IndexError, StopIteration) as e:
        return False, f"DEF: {e}"
    on = {l.split()[1]: l.split()[0] for l in lines_of(place_text)}
    if len(comps) != len(d["fab"]):
        return False, f"{len(comps)} components, the fabric has {len(d['fab'])} cells"
    for f, (typ, _, _) in d["fab"].items():
        nm = on.get(f, f)
        llx, lly, ori = d["fab_ll"][f]
        if comps.get(nm) != (typ, llx, lly, ori):
            return False, f"component {nm} (fabric cell {f}): got {comps.get(nm)}, expected {(typ, llx, lly, ori)}"
    for p, xy in d["pins"].items():
        q = pins.get(p[4:])
        if q is None or (q[1], q[2]) != xy:
            return False, f"pin {p[4:]} missing or misplaced"
    conn = lambda x: ("PIN", x[4:]) if x.startswith("pin:") else tuple(x.split("/"))
    on_net = {x[4:]: n for n, ts in d["raw_nets"] for x in ts if x.startswith("pin:")}
    for p in d["pins"]:
        q = pins.get(p[4:])
        if q[0] != on_net.get(p[4:], p[4:]):
            return False, f"pin {p[4:]}: + NET {q[0]}, expected {on_net.get(p[4:], p[4:])}"
    own = [(p[4:], [p]) for p in sorted(d["pins"]) if p[4:] not in on_net]
    for n, ts in d["raw_nets"] + own:
        if n == d["clock"] and clock_text is not None:
            continue
        if nets.get(n) != frozenset(conn(x) for x in ts):
            return False, f"net {n} missing or wrong connections"
    signal = {n for n, _ in d["raw_nets"] + own} - ({d["clock"]} if clock_text is not None else set())
    extra = {n: c for n, c in nets.items() if n not in signal}
    if clock_text is None:
        return (not extra), ("" if not extra else f"unexpected nets {sorted(extra)[:3]}")
    node, kids = {}, {}
    for l in lines_of(clock_text):
        t = l.split()
        if t[0] == "NODE":
            node[t[1]] = (t[2], t[3] if len(t) > 3 else None)
        else:
            kids.setdefault(t[1], []).append(t[2])
    root_pin = next(x[4:] for n, ts in d["raw_nets"] if n == d["clock"] for x in ts if x.startswith("pin:"))
    want = set()
    for nid, ch in kids.items():
        k, ref = node[nid]
        drv = ("PIN", root_pin) if k == "root" else (ref, "X")
        want.add(frozenset([drv] + [(node[c][1], "A" if node[c][0] == "buf" else "CLK") for c in ch]))
    if set(extra.values()) != want:
        return False, "clock nets do not match the clock tree (one net per driver: root pin or buffer X -> children)"
    if d["clock"] not in extra or ("PIN", root_pin) not in extra[d["clock"]]:
        return False, f"the root clock net must keep the name {d['clock']}"
    return True, f"{len(want)} clock nets match the tree"


def openroad_available():
    if shutil.which("openroad") and os.environ.get("PLATFORM_DIR"):
        return True
    if shutil.which("docker"):
        img = os.environ.get("ORFS_IMAGE", "openroad/orfs@sha256:751a77afcade9882b51427e6d9d079b8e270e7a8f4aa66df2d0659457d1c29fd")
        return subprocess.run(["docker", "image", "inspect", img], capture_output=True).returncode == 0
    return False


def sa_route(def_text):
    """Route the DEF with the provided route script.  Returns (ok, summary) or (None, reason)."""
    script = next((p for p in (os.path.join(HERE, "tools", "openroad", "or_route.sh"),
                               os.path.join(HERE, "openroad", "or_route.sh")) if os.path.isfile(p)), None)
    if script is None:
        return None, "or_route.sh not found"
    wd = tempfile.mkdtemp(dir=HERE, prefix=".grade_or_")
    try:
        open(os.path.join(wd, "design.def"), "w").write(def_text)
        p = subprocess.run(["sh", script, "design.def"], cwd=wd, capture_output=True, text=True, timeout=3600)
    finally:
        shutil.rmtree(wd, ignore_errors=True)
    out = p.stdout + p.stderr
    drc = re.search(r"^@@ drc_violations (\d+)", out, re.M)
    grt = re.search(r"GRT-0018\] Total wirelength: (\d+)", out)
    if re.search(r"^@@ global_route_failed 1", out, re.M):
        return False, "global routing failed (congestion)"
    if drc is None:
        err = [l for l in out.splitlines() if "ERROR" in l]
        return False, "routing did not finish: " + (err[0] if err else "no summary")
    n = int(drc.group(1))
    return n == 0, f"routed, {grt.group(1) if grt else '?'} um, {n} DRC violations"


def sa_openroad(def_text, sdc, propagated):
    """Run the provided STA script.  Returns (ok, summary)."""
    script = next((p for p in (os.path.join(HERE, "tools", "openroad", "or_sta.sh"),
                               os.path.join(HERE, "openroad", "or_sta.sh")) if os.path.isfile(p)), None)
    if script is None:
        return None, "or_sta.sh not found"
    wd = tempfile.mkdtemp(dir=HERE, prefix=".grade_or_")
    try:
        open(os.path.join(wd, "design.def"), "w").write(def_text)
        shutil.copy(sdc, os.path.join(wd, "design.sdc"))
        p = subprocess.run(["sh", script, "design.def", "design.sdc"] + (["propagated"] if propagated else []),
                           cwd=wd, capture_output=True, text=True, timeout=900)
    finally:
        shutil.rmtree(wd, ignore_errors=True)
    v = dict(re.findall(r"^@@ (\w+) (-?[0-9.]+)", p.stdout, re.M))
    if "setup_wns" not in v:
        err = [l for l in (p.stdout + p.stderr).splitlines() if "ERROR" in l or "Error" in l]
        return False, "OpenROAD could not time the DEF: " + (err[0] if err else "no summary")
    return True, (f"OpenROAD: setup WNS {v['setup_wns']} ns, hold WNS {v['hold_wns']} ns, "
                  f"clock skew {v['clock_skew']} ns, latency {v['clock_latency_max']} ns")


def grade_sasic(cp, cases, sub, tests, timeout, res):
    for c in cases:
        fdir = os.path.join(tests, c["fabric"])
        ddir = os.path.join(tests, c["input"]) if c.get("input") else None
        name = c["id"]
        if not os.path.isdir(fdir) or (ddir and not os.path.isdir(ddir)):
            res.add(name, None, "input not present")
            continue
        if c["check"] in ("fabric_exact", "report_exact"):
            cmd = "fabric" if c["check"] == "fabric_exact" else "report"
            out, err = run_student(sub, [cmd, fdir] + ([ddir] if cmd == "report" else []), timeout)
            if out is None:
                res.add(f"{name}/{cmd}", False, err, c.get("item"))
            else:
                exact(res, f"{name}/{cmd}", out, os.path.join(tests, c["expected"]), c.get("item"))
            continue
        d = sa_design(fdir, ddir)
        out, err = run_student(sub, ["place", fdir, ddir], timeout)
        pl, perr = (None, err) if out is None else sa_check_placement(d, out)
        res.add(f"{name}/placement_legal", pl is not None, perr, "place_legal")
        if c["check"] == "place_legal":
            continue
        exp = json.load(open(os.path.join(tests, c["expected"])))
        if pl is None:
            for it in ("twl", "clock_legal", "clock_wire"):
                res.add(f"{name}/{it}", False, "no legal placement", it)
            continue
        h, ref = sa_twl(d, pl), exp["ref_twl"]
        res.add(f"{name}/twl", h <= 1.25 * ref, f"TWL {h}, reference {ref}, bound {1.25 * ref:g}", "twl")
        with tempfile.NamedTemporaryFile("w", suffix=".place", delete=False) as f:
            f.write(out)
            plfile = f.name
        cout, cerr = run_student(sub, ["clock", fdir, ddir, plfile], timeout)
        os.unlink(plfile)
        if cout is None:
            for it in ("clock_legal", "clock_wire"):
                res.add(f"{name}/{it}", False, cerr, it)
            continue
        r = sa_check_clock(d, pl, cout)
        res.add(f"{name}/clock_legal", r["legal"], r["err"] or
                f"skew {r['skew']} <= {d['max_skew']}, latency {r['lat']} <= {d['max_latency']}", "clock_legal")
        if r["legal"]:
            res.add(f"{name}/clock_wire", r["wire"] <= 1.20 * exp["ref_clock_wire"],
                    f"wire {r['wire']}, reference {exp['ref_clock_wire']}, bound {1.20 * exp['ref_clock_wire']:g}",
                    "clock_wire")
        else:
            res.add(f"{name}/clock_wire", False, "clock illegal", "clock_wire")
        # DEF export: placement only (student A), then with the clock tree (student B)
        with tempfile.NamedTemporaryFile("w", suffix=".place", delete=False) as f:
            f.write(out)
            plfile = f.name
        with tempfile.NamedTemporaryFile("w", suffix=".clock", delete=False) as f:
            f.write(cout)
            ckfile = f.name
        dout, derr = run_student(sub, ["def", fdir, ddir, plfile], timeout)
        ok, msg = (False, derr) if dout is None else sa_check_def(d, out, dout)
        res.add(f"{name}/def_placement", ok, msg, "def_place")
        dout2, derr2 = run_student(sub, ["def", fdir, ddir, plfile, ckfile], timeout)
        os.unlink(plfile)
        os.unlink(ckfile)
        ok, msg = (False, derr2) if dout2 is None else sa_check_def(d, out, dout2, cout if r["legal"] else None)
        if ok and r["legal"] and USE_OPENROAD:
            ok2, msg2 = sa_openroad(dout2, os.path.join(ddir, "design.sdc"), True)
            if ok2 is not None:
                ok, msg = ok and ok2, f"{msg}; {msg2}"
        res.add(f"{name}/def_clock", ok and r["legal"], msg if r["legal"] else "clock illegal", "def_clock")
        if not c.get("route"):
            pass                                    # routing is graded on the 10 x 10 cases only
        elif not USE_OPENROAD:
            res.add(f"{name}/routable", None, "OpenROAD not available or --no-openroad", "route")
        elif not (ok and r["legal"]):
            res.add(f"{name}/routable", False, "needs a legal clock tree and a correct DEF", "route")
        else:
            rok, rmsg = sa_route(dout2)
            res.add(f"{name}/routable", rok, rmsg, "route")


# ---------------------------------------------------------------- macro placement

MP_ORIENTS = ["N", "S", "W", "E", "FN", "FS", "FW", "FE"]


def mp_legal_set(tok):
    t = set(tok)
    if t <= {"none"}:
        return {"N"}
    if "R90" in t:
        return set(MP_ORIENTS) if ("X" in t or "Y" in t) else {"N", "S", "W", "E"}
    return {frozenset({"X"}): {"N", "FS"}, frozenset({"Y"}): {"N", "FN"},
            frozenset({"X", "Y"}): {"N", "S", "FN", "FS"}}[frozenset(t)]


def mp_size(m, o):
    return (m["h"], m["w"]) if o in ("W", "E", "FW", "FE") else (m["w"], m["h"])


def mp_xform(x, y, w, h, X, Y, o):
    return {"N": (X + x, Y + y), "S": (X + w - x, Y + h - y), "W": (X + h - y, Y + x),
            "E": (X + y, Y + w - x), "FN": (X + w - x, Y + y), "FS": (X + x, Y + h - y),
            "FW": (X + y, Y + x), "FE": (X + h - y, Y + w - x)}[o]


def mp_parse(path):
    d = dict(halo=0, pads={}, mac={}, fixed={}, blk=[], nets=[])
    for t in tokens(path):
        k = t[0]
        if k == ".core":
            d["W"], d["H"] = int(t[1]), int(t[2])
        elif k == ".halo":
            d["halo"] = int(t[1])
        elif k == ".pad":
            d["pads"][t[1]] = (int(t[2]), int(t[3]))
        elif k == ".macro":
            d["mac"][t[1]] = dict(w=int(t[2]), h=int(t[3]), legal=mp_legal_set(t[4:]), pins={})
        elif k == ".pin":
            d["mac"][t[1]]["pins"][t[2]] = (int(t[3]), int(t[4]))
        elif k == ".fixed":
            d["fixed"][t[1]] = (int(t[2]), int(t[3]), t[4])
        elif k == ".blockage":
            d["blk"].append(tuple(map(int, t[1:5])))
        elif k == ".net":
            d["nets"].append((int(t[2]), t[3:]))
    d["movable"] = sorted(m for m in d["mac"] if m not in d["fixed"])
    return d


def mp_hpwl(d, pl):
    tot = 0
    for w, terms in d["nets"]:
        pts = []
        for t in terms:
            if t in d["pads"]:
                pts.append(d["pads"][t])
            else:
                m, pin = t.split(".", 1)
                mm = d["mac"][m]
                pts.append(mp_xform(*mm["pins"][pin], mm["w"], mm["h"], *pl[m]))
        if len(pts) > 1:
            tot += w * (max(p[0] for p in pts) - min(p[0] for p in pts) + max(p[1] for p in pts) - min(p[1] for p in pts))
    return tot


def mp_check_placement(d, text):
    pl, names = {}, []
    for i, l in enumerate(lines_of(text)):
        t = l.split()
        if len(t) != 4:
            return None, f"line {i + 1}: expected 'name X Y ORIENT'"
        try:
            p = (int(t[1]), int(t[2]), t[3])
        except ValueError:
            return None, f"line {i + 1}: non-integer coordinate"
        if t[0] not in d["mac"] or t[0] in pl:
            return None, f"line {i + 1}: unknown or repeated macro {t[0]}"
        pl[t[0]] = p
        names.append(t[0])
    if names != sorted(names):
        return None, "lines not sorted by name"
    if set(pl) != set(d["mac"]):
        return None, f"missing macros {sorted(set(d['mac']) - set(pl))}"
    G = d["halo"]
    box = {}
    for m, (X, Y, o) in pl.items():
        if o not in MP_ORIENTS:
            return None, f"{m}: bad orientation {o}"
        w, h = mp_size(d["mac"][m], o)
        box[m] = (X, Y, w, h)
        if m in d["fixed"]:
            if (X, Y, o) != d["fixed"][m]:
                return None, f"fixed macro {m} moved"
            continue
        if o not in d["mac"][m]["legal"]:
            return None, f"{m}: orientation {o} not allowed by SYMMETRY"
        if X < 0 or Y < 0 or X + w > d["W"] or Y + h > d["H"]:
            return None, f"{m} leaves the core"
        for b in d["blk"]:
            if X < b[0] + b[2] and b[0] < X + w and Y < b[1] + b[3] and b[1] < Y + h:
                return None, f"{m} overlaps a blockage"
    ms = sorted(pl)
    for i, a in enumerate(ms):
        A = box[a]
        for b in ms[i + 1:]:
            if a in d["fixed"] and b in d["fixed"]:
                continue
            B = box[b]
            if not (A[0] + A[2] + G <= B[0] or B[0] + B[2] + G <= A[0] or
                    A[1] + A[3] + G <= B[1] or B[1] + B[3] + G <= A[1]):
                return None, f"{a} and {b} are closer than the halo ({G})"
    return pl, ""


def mp_edges(d):
    """Clique model: (u, v, weight) over distinct objects of each net."""
    out = []
    for w, terms in d["nets"]:
        objs = []
        for t in terms:
            o = t if t in d["pads"] else t.split(".", 1)[0]
            if o not in objs:
                objs.append(o)
        k = len(objs)
        for i in range(k):
            for j in range(i + 1, k):
                out.append((objs[i], objs[j], w / (k - 1)))
    return out


def mp_anchor(d, o):
    if o in d["pads"]:
        return d["pads"][o]
    X, Y, ori = d["fixed"][o]
    w, h = mp_size(d["mac"][o], ori)
    return (X + w / 2, Y + h / 2)


def mp_phi(d, pos):
    tot = 0.0
    for u, v, e in mp_edges(d):
        a = pos[u] if u in pos else mp_anchor(d, u)
        b = pos[v] if v in pos else mp_anchor(d, v)
        tot += e * ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2)
    return tot


def mp_optimum(d):
    mov = d["movable"]
    ix = {m: i for i, m in enumerate(mov)}
    n = len(mov)
    sol = []
    for axis in (0, 1):
        M = [[0.0] * (n + 1) for _ in range(n)]
        for u, v, e in mp_edges(d):
            for a, b in ((u, v), (v, u)):
                if a in ix:
                    M[ix[a]][ix[a]] += e
                    if b in ix:
                        M[ix[a]][ix[b]] -= e
                    else:
                        M[ix[a]][n] += e * mp_anchor(d, b)[axis]
        for c in range(n):
            p = max(range(c, n), key=lambda r: abs(M[r][c]))
            M[c], M[p] = M[p], M[c]
            for r in range(n):
                if r != c:
                    f = M[r][c] / M[c][c]
                    M[r] = [x - f * y for x, y in zip(M[r], M[c])]
        sol.append([M[i][n] / M[i][i] for i in range(n)])
    return {m: (sol[0][i], sol[1][i]) for i, m in enumerate(mov)}


def grade_macro(cp, cases, sub, tests, timeout, res):
    for c in cases:
        inp = os.path.join(tests, c["input"])
        name = f"{c['student']}/{c['id']}"
        item = c.get("item")
        if not os.path.isfile(inp):
            res.add(name, None, "input not present")
            continue
        chk = c["check"]
        if chk in ("pack_exact", "eval_exact", "seed_exact"):
            cmd = {"pack_exact": "pack", "eval_exact": "eval", "seed_exact": "seed"}[chk]
            out, err = run_student(sub, [cmd, inp, os.path.join(tests, c["aux"])], timeout)
            if out is None:
                res.add(name, False, err, item)
            else:
                exact(res, name, out, os.path.join(tests, c["expected"]), item)
            continue
        d = mp_parse(inp)
        if chk == "quad_bound":
            out, err = run_student(sub, ["quad", inp], timeout)
            if out is None:
                res.add(name, False, err, item)
                continue
            pos, msg = {}, ""
            for l in lines_of(out):
                t = l.split()
                try:
                    if len(t) != 3 or t[0] not in d["movable"] or t[0] in pos:
                        raise ValueError
                    pos[t[0]] = (float(t[1]), float(t[2]))
                    if not all(math.isfinite(v) for v in pos[t[0]]):
                        raise ValueError
                except ValueError:
                    msg = f"bad line '{l}'"
                    break
            if not msg and set(pos) != set(d["movable"]):
                msg = f"missing macros {sorted(set(d['movable']) - set(pos))}"
            if msg:
                res.add(name, False, msg, item)
                continue
            phi, best = mp_phi(d, pos), mp_phi(d, mp_optimum(d))
            res.add(name, phi <= best * (1 + 1e-3) + 1e-6,
                    f"quadratic wirelength {phi:.2f}, optimum {best:.2f} (limit +0.1%)", item)
        elif chk == "place_bound":
            out, err = run_student(sub, ["place", inp], timeout)
            pl, perr = (None, err) if out is None else mp_check_placement(d, out)
            res.add(f"{name}/legal", pl is not None, perr, "legal")
            ref = json.load(open(os.path.join(tests, c["expected"])))["ref_hpwl"]
            if pl is None:
                res.add(f"{name}/hpwl", False, "placement illegal", "hpwl")
            else:
                h = mp_hpwl(d, pl)
                res.add(f"{name}/hpwl", h <= 1.25 * ref, f"HPWL {h}, reference {ref}, bound {1.25 * ref:g}", "hpwl")
    for st in ("A", "B"):
        rows = [r for r in res.rows if r[0].startswith(st + "/") and r[1] is not None]
        if rows:
            print(f"student {st}: {sum(1 for r in rows if r[1])}/{len(rows)} passed")


# ---------------------------------------------------------------- FM partitioner

def o4_parse(path):
    cells, nets = [], []
    for t in tokens(path):
        if t[0] == ".cells":
            cells += t[1:]
        elif t[0] == ".net":
            nets.append(set(t[2:]))
    return sorted(set(cells)), nets


def grade_fm(cp, cases, sub, tests, timeout, res):
    for c in cases:
        inp = os.path.join(tests, c["input"])
        if not os.path.isfile(inp):
            res.add(c["id"], None, "input not present")
            continue
        out, err = run_student(sub, [c["cmd"], inp], timeout)
        if out is None:
            res.add(c["id"], False, err)
            continue
        if c["check"] == "exact":
            exact(res, c["id"], out, os.path.join(tests, c["expected"]))
            continue
        cells, nets = o4_parse(inp)
        L = lines_of(out)
        try:
            assert len(L) == 3 and L[0].split()[0] == "CUT" and L[1].split()[0] == "PART0" and L[2].split()[0] == "PART1"
            k = int(L[0].split()[1])
            p0, p1 = L[1].split()[1:], L[2].split()[1:]
        except (AssertionError, IndexError, ValueError):
            res.add(c["id"], False, "output must be CUT / PART0 / PART1 lines")
            continue
        n = len(cells)
        lo, hi = math.floor(0.4 * n), math.ceil(0.6 * n)
        if sorted(p0 + p1) != cells or len(set(p0 + p1)) != n:
            res.add(c["id"], False, "every cell must appear exactly once")
            continue
        if p0 != sorted(p0) or p1 != sorted(p1):
            res.add(c["id"], False, "names not sorted")
            continue
        if not (lo <= len(p0) <= hi and lo <= len(p1) <= hi):
            res.add(c["id"], False, f"balance {len(p0)}/{len(p1)} outside [{lo}, {hi}]")
            continue
        s0 = set(p0)
        cut = sum(1 for e in nets if 0 < len(e & s0) < len(e))
        if cut != k:
            res.add(c["id"], False, f"reported CUT {k} but the partition cuts {cut} nets")
            continue
        ref = json.load(open(os.path.join(tests, c["expected"])))["ref_cut"]
        bound = ref + max(1, math.floor(0.15 * ref))
        res.add(c["id"], cut <= bound, f"cut {cut}, reference {ref}, bound {bound}")


# ---------------------------------------------------------------- maze router (3 layers)

O5_HORIZONTAL = {1: True, 2: False, 3: True}


def o5_parse(path):
    W = H = 0
    vcost = 1
    blocks, nets = set(), []
    for t in tokens(path):
        if t[0] == ".grid":
            W, H = int(t[1]), int(t[2])
        elif t[0] == ".via_cost":
            vcost = int(t[1])
        elif t[0] == ".block":
            layers = (1, 2, 3) if t[1] == "*" else (int(t[1]),)
            x1, y1, x2, y2 = map(int, t[2:6])
            blocks |= {(x, y, l) for l in layers for x in range(min(x1, x2), max(x1, x2) + 1)
                       for y in range(min(y1, y2), max(y1, y2) + 1)}
        elif t[0] == ".net":
            xy = list(map(int, t[2:]))
            nets.append((t[1], [(xy[k], xy[k + 1], 1) for k in range(0, len(xy), 2)]))
    return W, H, vcost, blocks, nets


def o5_check(W, H, vcost, blocks, nets, text):
    """Check a multi-branch routing against the model and DR1-DR3.  Returns (total cost, error)."""
    L = lines_of(text)
    routes, i = {}, 0
    while i < len(L):
        t = L[i].split()
        if len(t) != 2 or t[0] != "NET":
            return None, f"line {i + 1}: expected NET"
        name = t[1]
        i += 1
        branches = []
        while i < len(L) and L[i] != "END":
            if L[i] == "BRANCH":
                branches.append([])
            elif not branches:
                return None, f"line {i + 1}: points before the first BRANCH"
            else:
                try:
                    x, y, l = map(int, L[i].split())
                except ValueError:
                    return None, f"line {i + 1}: expected 'x y layer'"
                branches[-1].append((x, y, l))
            i += 1
        if i >= len(L):
            return None, "missing END"
        i += 1
        t = L[i].split() if i < len(L) else []
        if len(t) != 4 or t[0] != "WL" or t[2] != "VIAS":
            return None, f"net {name}: missing 'WL n VIAS v' line"
        try:
            wl, nv = int(t[1]), int(t[3])
        except ValueError:
            return None, f"net {name}: bad WL/VIAS numbers"
        i += 1
        if name in routes:
            return None, f"net {name} routed twice"
        routes[name] = (branches, wl, nv)
    pins = {}
    for n, ps in nets:
        for q in ps:
            pins[q] = n
    owner, vias, total = {}, [], 0
    for n, ps in nets:
        if n not in routes:
            return None, f"net {n} not routed"
        branches, wl, nv = routes[n]
        if len(branches) != len(ps) - 1:
            return None, f"net {n}: {len(ps)} pins need {len(ps) - 1} branches, got {len(branches)}"
        if not branches[0] or branches[0][0] not in ps:
            return None, f"net {n}: the first branch must start at one of its pins"
        tree, connected = {branches[0][0]}, {branches[0][0]}
        steps = vsteps = 0
        for b in branches:
            if len(b) < 2 or b[0] not in tree:
                return None, f"net {n}: a branch must start on the net's existing tree"
            if b[-1] not in ps or b[-1] in connected:
                return None, f"net {n}: a branch must end at a pin not yet connected"
            for k, p in enumerate(b):
                x, y, l = p
                if k and p in tree:
                    return None, f"net {n} revisits {p}"
                if not (0 <= x < W and 0 <= y < H and 1 <= l <= 3):
                    return None, f"net {n} leaves the grid at {p}"
                if p in blocks:
                    return None, f"net {n} enters a block at {p}"
                if pins.get(p, n) != n:
                    return None, f"DR1: net {n} uses a pin of net {pins[p]} at {p}"
                if owner.get(p, n) != n:
                    return None, f"DR1: nets {owner[p]} and {n} share {p}"
                if 0 < k < len(b) - 1 and p in ps and p not in connected:
                    return None, f"net {n}: a branch passes through its unconnected pin {p}"
                owner[p] = n
                if k:
                    q = b[k - 1]
                    d = (abs(p[0] - q[0]), abs(p[1] - q[1]), abs(p[2] - q[2]))
                    if d == (0, 0, 1):
                        vsteps += 1
                        vias.append(((x, y, min(l, q[2])), n))
                    elif d in ((1, 0, 0), (0, 1, 0)):
                        if (d[0] == 1) != O5_HORIZONTAL[l]:
                            return None, f"DR2: net {n} runs against M{l}'s preferred direction at {q} -> {p}"
                        steps += 1
                    else:
                        return None, f"net {n} jumps between {q} and {p}"
                tree.add(p)
            connected.add(b[-1])
        if connected != set(ps):
            return None, f"net {n}: pins {sorted(set(ps) - connected)} not connected"
        if (wl, nv) != (steps, vsteps):
            return None, f"net {n}: reported WL {wl} VIAS {nv}, routes have WL {steps} VIAS {vsteps}"
        total += steps + vcost * vsteps
    for k, (v, n) in enumerate(vias):
        for w, m in vias[k + 1:]:
            if n != m and v[2] == w[2] and max(abs(v[0] - w[0]), abs(v[1] - w[1])) <= 1:
                return None, f"DR3: vias of {n} at {v[:2]} and {m} at {w[:2]} on cut {v[2]}-{v[2] + 1} are too close"
    extra = set(routes) - {n for n, _ in nets}
    if extra:
        return None, f"unknown nets {sorted(extra)}"
    return total, ""


def grade_maze(cp, cases, sub, tests, timeout, res):
    for c in cases:
        inp = os.path.join(tests, c["input"])
        if not os.path.isfile(inp):
            res.add(c["id"], None, "input not present")
            continue
        out, err = run_student(sub, ["route", inp], timeout)
        if out is None:
            res.add(c["id"], False, err)
            continue
        if c["check"] == "exact":
            exact(res, c["id"], out, os.path.join(tests, c["expected"]))
            continue
        W, H, vcost, blocks, nets = o5_parse(inp)
        total, msg = o5_check(W, H, vcost, blocks, nets, out)
        if total is None:
            res.add(c["id"], False, "illegal: " + msg)
            continue
        ref = json.load(open(os.path.join(tests, c["expected"])))["ref_cost"]
        res.add(c["id"], total <= 1.15 * ref, f"total cost {total} (WL + {vcost} x vias), reference {ref}, "
                f"bound {1.15 * ref:.2f}")


# ---------------------------------------------------------------- tree covering

def grade_cover(cp, cases, sub, tests, timeout, res):
    for c in cases:
        inp = os.path.join(tests, c["input"])
        if not os.path.isfile(inp):
            res.add(c["id"], None, "input not present")
            continue
        out, err = run_student(sub, [c.get("cmd", "map"), inp], timeout)
        if out is None:
            res.add(c["id"], False, err)
        else:
            exact(res, c["id"] + ("/timing" if c.get("cmd") == "delay" else ""), out,
                  os.path.join(tests, c["expected"]))


# ---------------------------------------------------------------- visualization (every option)

SHAPES = ("rect", "circle", "ellipse", "line", "polyline", "polygon", "path", "text")


def check_svg(text, min_shapes, labels=()):
    """Well-formed SVG with at least min_shapes shapes, and every name in labels shown in a <text>."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        return False, f"not well-formed XML: {e}"
    if not root.tag.endswith("svg"):
        return False, f"root element is <{root.tag}>, not <svg>"
    if "viewBox" not in root.attrib and not ("width" in root.attrib and "height" in root.attrib):
        return False, "the <svg> element needs a viewBox or a width and height"
    n = sum(1 for e in root.iter() if e.tag.split("}")[-1] in SHAPES)
    if n < min_shapes:
        return False, f"only {n} shapes for {min_shapes} objects"
    words = set()
    for e in root.iter():
        if e.tag.split("}")[-1] in ("text", "tspan", "title"):
            words.update(re.split(r"[\s(),:;=]+", "".join(e.itertext())))
    missing = sorted(set(labels) - words)
    if missing:
        return False, f"{len(missing)} of {len(labels)} names not labelled, e.g. {missing[:3]}"
    return True, f"{n} shapes, all {len(labels)} names labelled" if labels else f"{n} shapes"


def grade_draw(opt, c, sub, tests, timeout, res, svg_out):
    """Run the student's own solver, then their draw command on its output, and check the SVG."""
    inp = os.path.join(tests, c["input"])
    name = c["id"]
    if not os.path.exists(inp):
        res.add(name, None, "input not present", "draw")
        return
    tmp = []

    def run(args):
        out, err = run_student(sub, args, timeout)
        if out is None:
            raise RuntimeError(err)
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write(out)
        tmp.append(f.name)
        return f.name
    try:
        labels = ()
        if opt == "sasic":
            fdir = os.path.join(tests, c["fabric"])
            d = sa_design(fdir, inp)
            n = len(d["fab"])                      # every fabric cell drawn
            pl = run(["place", fdir, inp])
            args = ["draw", fdir, inp, pl, run(["clock", fdir, inp, pl])]
        elif opt == "macro":
            labels = set(mp_parse(inp)["mac"])
            n = len(labels)
            args = ["draw", inp, run(["place", inp])]
        elif opt == "fm":
            labels = set(o4_parse(inp)[0])
            n = len(labels)
            args = ["draw", inp, run(["part", inp])]
        elif opt == "maze":
            labels = {nm for nm, _ in o5_parse(inp)[4]}
            n = len(labels)
            args = ["draw", inp, run(["route", inp])]
        else:
            labels = {t[1] for t in tokens(inp) if t[0] == ".node"}
            n = len(labels)
            args = ["draw", inp, run(["map", inp])]
        svg, err = run_student(sub, args, timeout)
    except RuntimeError as e:
        svg, err = None, f"producing the input for draw failed: {e}"
    finally:
        for f in tmp:
            os.unlink(f)
    if svg is None:
        res.add(name, False, err, "draw")
        return
    ok, msg = check_svg(svg, n, labels)
    if svg_out:
        os.makedirs(os.path.join(svg_out, opt), exist_ok=True)
        path = os.path.join(svg_out, opt, name + ".svg")
        open(path, "w").write(svg)
        msg += f"; saved to {path}"
    res.add(name, ok, msg, "draw")


# ---------------------------------------------------------------- main

USE_OPENROAD = False
GRADERS = {"sasic": grade_sasic, "macro": grade_macro, "fm": grade_fm, "maze": grade_maze, "cover": grade_cover}

# Project weights: core cases 10, full cases 30, visualization 10, all automatic (the intermediate
# checkpoint is a separate run of the core cases, worth another 10);
# slides 15, GitHub history 10 and the final interview 15 are marked by hand.
CORE_PTS, FULL_PTS, VIS_PTS = 10, 30, 10
# Options 1 and 2 split the full-case points by item; options 3-5 share them equally over the cases.
FULL_ITEMS = {"sasic": {"hidden_place": 6, "twl": 3, "clock_legal": 6, "clock_wire": 3, "fabric": 3,
                        "def_place": 3, "def_clock": 3, "route": 3},
              "macro": {"hidden_legal": 9, "hpwl": 9, "seed": 12}}


def frac(rows):
    v = [bool(r[1]) for r in rows if r[1] is not None]
    return sum(v), len(v)


NOT_RUN = "  (no cases run here; left out of the total)"


def full_score(opt, rows, cases):
    """(points, points available) for the full cases, draw rows excluded.  An item with no
    case run (hidden cases missing from a student copy, or OpenROAD off) is not available."""
    rows = [r for r in rows if r[1] is not None and r[3] != "draw"]
    if opt not in FULL_ITEMS:
        p, n = frac(rows)
        print(f"  {'full cases':19s} {f'{p}/{n}':>7s}  {FULL_PTS * p / max(1, n):5.1f} / {FULL_PTS}" + ("" if n else NOT_RUN))
        return FULL_PTS * p / max(1, n), FULL_PTS if n else 0
    hidden = [c["id"] for c in cases if c["hidden"]]
    got = {}
    for case, ok, _, item in rows:
        if item in ("place_legal", "legal"):           # legality counts on the hidden designs only
            if not any(case.startswith(h + "/") or case.startswith(f"A/{h}/") for h in hidden):
                continue
            item = "hidden_place" if opt == "sasic" else "hidden_legal"
        got.setdefault(item, []).append(bool(ok))
    tot = avail = 0
    for k, w in FULL_ITEMS[opt].items():
        v = got.get(k, [])
        sc = w * sum(v) / len(v) if v else 0
        tot += sc
        avail += w if v else 0
        print(f"  {'full ' + k:19s} {f'{sum(v)}/{len(v)}':>7s}  {sc:5.1f} / {w}" + ("" if v else NOT_RUN))
    return tot, avail


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("option", choices=list(GRADERS))
    ap.add_argument("--sub", required=True)
    ap.add_argument("--only", choices=["core", "full"], help="run one part only; --only core is the intermediate checkpoint")
    ap.add_argument("--tests", default=os.path.join(HERE, "tests"))
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--no-openroad", action="store_true", help="sasic: skip the OpenROAD timing and routing runs")
    ap.add_argument("--svg-out", default=None, help="save every visualization the student's draw command produces here")
    a = ap.parse_args()
    man = json.load(open(os.path.join(a.tests, "manifest.json")))
    if a.option not in man:
        raise SystemExit(f"this package has no tests for option '{a.option}' (it has: {', '.join(man)})")
    parts = [a.only] if a.only else ["core", "full"]
    tests = os.path.join(os.path.abspath(a.tests), a.option)
    sub = os.path.abspath(a.sub)
    what = {None: "final submission", "core": "core cases (the intermediate checkpoint)", "full": "full cases only"}[a.only]
    print(f"== {a.option} {what}  submission: {sub}")
    global USE_OPENROAD
    USE_OPENROAD = a.option == "sasic" and "full" in parts and not a.no_openroad and openroad_available()
    if a.option == "sasic" and "full" in parts:
        print("OpenROAD timing and routing of the clock-tree DEF: " + ("on" if USE_OPENROAD else "off (not available or --no-openroad)"))
    results = {}
    for part in parts:
        cases = man[a.option][part]
        print(f"\n-- {part} cases")
        res = results[part] = Result()
        GRADERS[a.option](part, [c for c in cases if c["check"] != "draw"], sub, tests, a.timeout, res)
        draws = [c for c in cases if c["check"] == "draw"]
        if draws:
            print("\nVisualization (run.py draw):")
            for c in draws:
                grade_draw(a.option, c, sub, tests, a.timeout, res, a.svg_out)

    print("\nAutomatic score (slides and presentation 15, GitHub history 10 and the interview 15 are marked by hand;"
          "\n the intermediate checkpoint's 10 come from a run of --only core on November 26):")
    total = avail = 0
    if "core" in results:
        p, n = frac([r for r in results["core"].rows if r[3] != "draw"])
        total += CORE_PTS * p / max(1, n)
        avail += CORE_PTS if n else 0
        print(f"  {'core cases':19s} {f'{p}/{n}':>7s}  {CORE_PTS * p / max(1, n):5.1f} / {CORE_PTS}" + ("" if n else NOT_RUN))
    if "full" in results:
        sc, av = full_score(a.option, results["full"].rows, man[a.option]["full"])
        total, avail = total + sc, avail + av
        p, n = frac([r for r in results["full"].rows if r[3] == "draw"])
        total += VIS_PTS * p / max(1, n)
        avail += VIS_PTS if n else 0
        print(f"  {'visualization':19s} {f'{p}/{n}':>7s}  {VIS_PTS * p / max(1, n):5.1f} / {VIS_PTS}" + ("" if n else NOT_RUN)
              + ("  (the pictures themselves are judged in the slides)" if n and a.option in ("sasic", "macro") else ""))
    top = CORE_PTS + FULL_PTS + VIS_PTS
    print(f"  {'total':19s}          {total:5.1f} / {avail}" + ("" if a.only or avail == top
          else f"   (of {top}; the items not run here are added at grading)"))
    rows = [r for res in results.values() for r in res.rows]
    done = [r for r in rows if r[1] is not None]
    passed = sum(1 for r in done if r[1])
    skipped = len(rows) - len(done)
    print(f"\n{a.option} {what}: {passed}/{len(done)} passed" + (f", {skipped} skipped" if skipped else ""))
    sys.exit(0 if passed == len(done) else 1)


if __name__ == "__main__":
    main()
