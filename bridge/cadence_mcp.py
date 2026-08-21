#!/usr/bin/env python3
"""
cadence_mcp.py — MCP server exposing OrCAD Capture 17.4 as agent tools.

Wraps capture_bridge.py so any MCP client can query and (optionally) modify a
live Capture design through structured tool calls, rather than screenshots.

    Client  --JSON-RPC/stdio-->  this server  --TCP 9020-->  Capture

DEPENDENCY-FREE BY DESIGN. MCP over stdio is newline-delimited JSON-RPC 2.0,
which the standard library covers, so there is nothing to pip install on a
machine whose main job is running EDA tools.

USAGE
    python bridge/cadence_mcp.py                 # read-only (default)
    python bridge/cadence_mcp.py --allow-write   # enables mutation tools

Register with Claude Code:
    claude mcp add cadence -- python C:/path/to/bridge/cadence_mcp.py

PREREQUISITE — Capture must be running with the server started:
    package require capCommServer
    ::capCommServer::StartServer
(or deploy tcl/capAutoLoad/capBridgeServerInit.tcl to start it automatically,
loopback-only, on every launch.)

WRITE TOOLS ARE OFF BY DEFAULT. An agent that can silently alter a schematic
is a materially different risk from one that can only read it, so mutation is
opt-in per invocation rather than a runtime argument the model can set.
stdout carries protocol traffic ONLY; all diagnostics go to stderr.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import sys
import traceback
from typing import Any, Callable

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from bridge.capture_bridge import (  # noqa: E402
    CaptureBridge,
    CaptureBridgeError,
    parse_tcl_list,
)
from bridge.allegro.allegro_client import (  # noqa: E402
    AllegroBridge,
    AllegroBridgeError,
)

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "cadence-mcp", "version": "0.2.0"}

BRIDGE_DIR = Path(__file__).resolve().parent
REPO_DIR = BRIDGE_DIR.parent
WORKFLOWS_TCL = REPO_DIR / "tcl" / "pcbWorkflows" / "pcbWorkflows.tcl"
QUERY_TCL = BRIDGE_DIR / "tcl" / "capBridgeQuery.tcl"
WIRE_TCL = BRIDGE_DIR / "tcl" / "capPlaceWire.tcl"
QUERY_IL = BRIDGE_DIR / "allegro" / "allegroQuery.il"


def log(msg: str) -> None:
    """Diagnostics to stderr. stdout is reserved for JSON-RPC."""
    print(f"[cadence-mcp] {msg}", file=sys.stderr, flush=True)


class Session:
    """Lazily-connected bridge that re-sources the query layer as needed.

    Capture may be restarted independently of this process, so the connection
    is established on demand and torn down on error rather than held open and
    assumed healthy.
    """

    def __init__(self) -> None:
        self._cap: CaptureBridge | None = None

    def get(self) -> CaptureBridge:
        if self._cap is None:
            cap = CaptureBridge(timeout=120)
            cap.ping()
            # Source repository-local dependencies explicitly. The previous
            # implementation assumed pcbWorkflows had already been copied into
            # Cadence's global tclscripts directory, so a fresh clone could
            # connect but every real MCP call failed while loading QUERY_TCL.
            for script in (WORKFLOWS_TCL, QUERY_TCL, WIRE_TCL):
                if not script.exists():
                    raise CaptureBridgeError(f"required Capture script is missing: {script}")
                cap.source_file(str(script).replace("\\", "/"))
            self._cap = cap
        return self._cap

    def reset(self) -> None:
        if self._cap is not None:
            try:
                self._cap.close()
            except Exception:
                pass
        self._cap = None


SESSION = Session()


@dataclass
class ToolOutput:
    """Text for humans plus structured content for agents."""

    text: str
    data: dict[str, Any]
    is_error: bool = False


def _rows(raw: str, proc: str) -> list[list[str]]:
    elements = parse_tcl_list(raw)
    if elements and elements[0] == "ERROR":
        raise CaptureBridgeError(f"{proc}: {' '.join(elements[1:])}")
    return [parse_tcl_list(row) for row in elements[1:]]


def _page(items: list[Any], args: dict, default: int = 100) -> tuple[list[Any], dict]:
    """Bound large EDA result sets so one tool call cannot flood context."""

    offset = int(args.get("offset", 0))
    limit = int(args.get("limit", default))
    if offset < 0:
        raise ValueError("offset must be >= 0")
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    total = len(items)
    selected = items[offset : offset + limit]
    next_offset = offset + len(selected)
    meta = {
        "total_count": total,
        "count": len(selected),
        "offset": offset,
        "limit": limit,
        "has_more": next_offset < total,
        "next_offset": next_offset if next_offset < total else None,
    }
    return selected, meta


PAGING_SCHEMA = {
    "offset": {"type": "integer", "minimum": 0, "default": 0},
    "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
}


# --------------------------------------------------------------------------
# Tool implementations return either legacy text or ToolOutput with both human
# text and machine-readable structuredContent.
# --------------------------------------------------------------------------

def t_status(_: dict) -> ToolOutput:
    cap = SESSION.get()
    data: dict[str, Any] = {
        "tcl_version": cap.tcl_version(),
        "dbo_command_count": cap.dbo_command_count(),
        "active_design": cap.has_active_design(),
    }
    lines = [
        f"tcl version   : {data['tcl_version']}",
        f"Dbo* commands : {data['dbo_command_count']}",
        f"active design : {data['active_design']}",
    ]
    try:
        info = parse_tcl_list(cap.call("::capBridge::designInfo"))
        if info and info[0] == "OK":
            data.update(
                design=info[1],
                schematic_count=int(info[2]),
                page_count=int(info[3]),
                part_count=int(info[4]),
            )
            lines += [
                f"design        : {info[1]}",
                f"schematics    : {info[2]}",
                f"pages         : {info[3]}",
                f"parts         : {info[4]}",
            ]
    except CaptureBridgeError as exc:
        lines.append(f"designInfo    : {exc}")
        data["design_info_error"] = str(exc)
    return ToolOutput("\n".join(lines), data)


def t_parts(args: dict) -> ToolOutput:
    parts = SESSION.get().parts()
    page = args.get("page")
    if page:
        parts = [p for p in parts if p["page"] == page]
    parts, meta = _page(parts, args)
    if not parts:
        return ToolOutput("No parts matched.", {"items": [], "pagination": meta})
    hdr = f"{'REFDES':8} {'PAGE':14} {'VALUE':14} {'FOOTPRINT':22} PART NUMBER"
    rows = [
        f"{p['refdes']:8} {p['page']:14} {p['value']:14} {p['footprint']:22} {p['part_number']}"
        for p in parts
    ]
    return ToolOutput(
        f"{meta['count']} of {meta['total_count']} parts\n\n" + hdr + "\n" + "\n".join(rows),
        {"items": parts, "pagination": meta},
    )


def t_nets(args: dict) -> ToolOutput:
    nets = SESSION.get().nets()
    all_nets = nets
    nets, meta = _page(nets, args)
    rows = "\n".join(f"{n['net']:16} {n['pin_count']:>3} pins" for n in nets)
    orphans = [n["net"] for n in all_nets if n["pin_count"] == 0]
    single = [n["net"] for n in all_nets if n["pin_count"] == 1]
    out = f"{meta['count']} of {meta['total_count']} nets\n\n{rows}"
    if orphans:
        out += f"\n\nORPHANED (0 pins): {', '.join(orphans)}"
    if single:
        out += f"\nSINGLE-NODE (1 pin): {', '.join(single)}"
    return ToolOutput(
        out,
        {"items": nets, "orphaned": orphans, "single_node": single, "pagination": meta},
    )


def t_connectivity(args: dict) -> ToolOutput:
    conns = SESSION.get().connectivity()
    want = args.get("net")
    if want:
        conns = [c for c in conns if c["net"] == want]
        if not conns:
            return ToolOutput(f"No net named {want!r}.", {"items": [], "pagination": {
                "total_count": 0, "count": 0, "offset": 0, "limit": int(args.get("limit", 100)),
                "has_more": False, "next_offset": None,
            }})
    conns.sort(key=lambda x: -int(x["pin_count"]))
    conns, meta = _page(conns, args)
    rows = []
    for c in conns:
        refs = ", ".join(sorted(set(c["refdes"])))
        rows.append(f"{c['net']:16} {c['pin_count']:>3} pins   {refs}")
    return ToolOutput(
        f"{meta['count']} of {meta['total_count']} nets\n\n" + "\n".join(rows),
        {"items": conns, "pagination": meta},
    )


def t_part_properties(args: dict) -> str:
    refdes = args["refdes"]
    raw = SESSION.get().call("::capBridge::partProps", [refdes])
    els = parse_tcl_list(raw)
    if els and els[0] == "ERROR":
        return f"ERROR: {' '.join(els[1:])}"
    rows = []
    for r in els[1:]:
        f = parse_tcl_list(r)
        value = f[1] if len(f) > 1 else ""
        rows.append(f"  {f[0]:24} = {value!r}")
    return f"Properties of {refdes}:\n" + "\n".join(rows)


def t_hanging_wires(args: dict) -> ToolOutput:
    els = parse_tcl_list(SESSION.get().call("::capBridge::hangingWires"))
    if els and els[0] == "ERROR":
        raise CaptureBridgeError(f"hangingWires: {' '.join(els[1:])}")
    rows = [parse_tcl_list(r) for r in els[1:]]
    items = [
        {"page": r[0], "x_db": int(r[1]), "y_db": int(r[2]), "end": r[3]}
        for r in rows
        if len(r) >= 4 and (not args.get("page") or r[0] == args["page"])
    ]
    items, meta = _page(items, args)
    if not items:
        return ToolOutput("No hanging wire endpoints found.", {"items": [], "pagination": meta})
    out = [f"{meta['count']} of {meta['total_count']} hanging wire endpoint(s):", ""]
    for item in items:
        # Capture schematic coordinates are internal units, conventionally
        # 1/100 inch. Shown both ways; treat the inch figure as indicative.
        try:
            inches = f"  (~{item['x_db']/100:.2f}in, {item['y_db']/100:.2f}in)"
        except ValueError:
            inches = ""
        out.append(
            f"  page={item['page']:14} x={item['x_db']:>6} y={item['y_db']:>6}  {item['end']}{inches}"
        )
    out.append(
        "\nA wire whose BOTH endpoints appear here, a unit or two apart, is a "
        "stray fragment — the usual cause of an orphaned (0-pin) auto-named net."
    )
    return ToolOutput("\n".join(out), {"items": items, "pagination": meta})


def t_pages(args: dict) -> ToolOutput:
    rows = _rows(SESSION.get().call("::capBridge::pages"), "pages")
    items = [
        {
            "schematic": row[0],
            "page": row[1],
            "part_count": int(row[2]),
        }
        for row in rows
        if len(row) >= 3
    ]
    items, meta = _page(items, args, default=50)
    lines = [f"{meta['count']} of {meta['total_count']} pages", ""]
    lines += [f"  {item['schematic']}/{item['page']}: {item['part_count']} parts" for item in items]
    return ToolOutput("\n".join(lines), {"items": items, "pagination": meta})


def t_pins(args: dict) -> ToolOutput:
    rows = _rows(SESSION.get().call("::capBridge::pins"), "pins")
    items: list[dict[str, Any]] = []
    for row in rows:
        if len(row) < 6:
            continue
        item: dict[str, Any] = {
            "schematic": row[0],
            "page": row[1],
            "refdes": row[2],
            "pin": row[3],
            "x_db": int(row[4]) if row[4].lstrip("-").isdigit() else row[4],
            "y_db": int(row[5]) if row[5].lstrip("-").isdigit() else row[5],
        }
        if args.get("page") and item["page"] != args["page"]:
            continue
        if args.get("refdes") and item["refdes"] != args["refdes"]:
            continue
        items.append(item)
    items, meta = _page(items, args)
    lines = [f"{meta['count']} of {meta['total_count']} pins", ""]
    lines += [
        f"  {p['page']}/{p['refdes']}.{p['pin']}: ({p['x_db']}, {p['y_db']}) db units"
        for p in items
    ]
    return ToolOutput("\n".join(lines), {"items": items, "pagination": meta})


def t_locate_net(args: dict) -> ToolOutput:
    net = args["net"]
    raw = SESSION.get().call("::capBridge::locateNet", [net])
    elements = parse_tcl_list(raw)
    if elements and elements[0] == "ERROR":
        raise CaptureBridgeError(f"locateNet: {' '.join(elements[1:])}")
    if len(elements) >= 3 and elements[1] == "NOT-FOUND-ON-ANY-WIRE":
        return ToolOutput(
            f"Net {net!r} is present in the flattened design but was not found on page wire geometry.",
            {"net": net, "locations": [], "found_on_wire": False},
        )
    locations = []
    for encoded in elements[1:]:
        row = parse_tcl_list(encoded)
        if len(row) >= 2:
            locations.append({"page": row[0], "wire_count": int(row[1])})
    text = f"Net {net!r}:\n" + "\n".join(
        f"  {loc['page']}: {loc['wire_count']} wire segment(s)" for loc in locations
    )
    return ToolOutput(text, {"net": net, "locations": locations, "found_on_wire": bool(locations)})


def t_library_packages(args: dict) -> ToolOutput:
    # absolute() preserves a mapped-drive spelling such as V:\\... . resolve()
    # expands it to UNC, but Capture keys open libraries by the exact spelling
    # used when they were loaded and has crashed on a mismatched UNC query.
    lib_path = str(Path(args["library_path"]).expanduser().absolute())
    rows = parse_tcl_list(SESSION.get().call("::capBridge::libPackages", [lib_path]))
    if rows and rows[0] == "ERROR":
        raise CaptureBridgeError(f"libPackages: {' '.join(rows[1:])}")
    items, meta = _page(rows[1:], args)
    return ToolOutput(
        f"{meta['count']} of {meta['total_count']} packages in {lib_path}\n\n" + "\n".join(f"  {x}" for x in items),
        {"library_path": lib_path, "items": items, "pagination": meta},
    )


def t_package_info(args: dict) -> ToolOutput:
    lib_path = str(Path(args["library_path"]).expanduser().absolute())
    elements = parse_tcl_list(
        SESSION.get().call("::capBridge::pkgInfo", [lib_path, args["package"]])
    )
    if elements and elements[0] == "ERROR":
        raise CaptureBridgeError(f"pkgInfo: {' '.join(elements[1:])}")
    data = {elements[i]: elements[i + 1] for i in range(1, len(elements) - 1, 2)}
    data.update(library_path=lib_path, package=args["package"])
    return ToolOutput(
        f"Package {args['package']}\n" + "\n".join(f"  {k}: {v}" for k, v in data.items()),
        data,
    )


def t_open_libraries(args: dict) -> ToolOutput:
    elements = parse_tcl_list(SESSION.get().call("::capBridge::openLibs"))
    if elements and elements[0] == "ERROR":
        raise CaptureBridgeError(f"openLibs: {' '.join(elements[1:])}")
    items, meta = _page(elements[1:], args, default=100)
    return ToolOutput(
        f"{meta['count']} of {meta['total_count']} open Capture libraries\n\n"
        + "\n".join(f"  {path}" for path in items),
        {"items": items, "pagination": meta},
    )


def t_engineering_audit(args: dict) -> ToolOutput:
    """One bounded, agent-friendly schematic sign-off summary."""

    cap = SESSION.get()
    max_findings = int(args.get("max_findings", 50))
    parts = cap.parts()
    nets = cap.nets()
    hanging = _rows(cap.call("::capBridge::hangingWires"), "hangingWires")
    workflows: dict[str, dict[str, Any]] = {}
    for name in CaptureBridge.WORKFLOWS:
        lines = cap.run_workflow(name)
        triage = cap.triage(lines)
        summary = next((line.strip() for line in lines if "SUMMARY:" in line), "")
        workflows[name] = {
            "summary": summary,
            "errors": triage["errors"][:max_findings],
            "warnings": triage["warnings"][:max_findings],
            "finding_count": len(triage["errors"]) + len(triage["warnings"]),
        }
        if name == "bomScrubber":
            bom = [line.strip() for line in lines if "Missing:" in line]
            workflows[name]["findings"] = bom[:max_findings]
            workflows[name]["finding_count"] = len(bom)

    data = {
        "part_count": len(parts),
        "net_count": len(nets),
        "orphaned_nets": [n["net"] for n in nets if n["pin_count"] == 0],
        "single_node_nets": [n["net"] for n in nets if n["pin_count"] == 1],
        "hanging_endpoint_count": len(hanging),
        "hanging_endpoints": [
            {"page": r[0], "x_db": int(r[1]), "y_db": int(r[2]), "end": r[3]}
            for r in hanging[:max_findings]
            if len(r) >= 4
        ],
        "workflows": workflows,
        "truncated_at": max_findings,
    }
    lines = [
        "Capture engineering audit",
        f"  parts: {data['part_count']}",
        f"  nets: {data['net_count']}",
        f"  orphaned nets: {len(data['orphaned_nets'])}",
        f"  single-node nets: {len(data['single_node_nets'])}",
        f"  hanging endpoints: {data['hanging_endpoint_count']}",
        "",
    ]
    for name, report in workflows.items():
        lines.append(f"  {name}: {report['summary'] or str(report['finding_count']) + ' finding(s)'}")
    return ToolOutput("\n".join(lines), data)


def _connection(args: dict, apply: bool) -> ToolOutput:
    values = [args["refdes_a"], args["pin_a"], args["refdes_b"], args["pin_b"], str(apply).lower()]
    elements = parse_tcl_list(SESSION.get().call("::capBridge::placeWireBetweenPins", values))
    if elements and elements[0] == "ERROR":
        raise CaptureBridgeError(f"placeWireBetweenPins: {' '.join(elements[1:])}")
    data = {"applied": apply, "result": elements[1:]}
    return ToolOutput(" ".join(elements[1:]), data)


def t_preview_connection(args: dict) -> ToolOutput:
    return _connection(args, apply=False)


def t_connect_pins(args: dict) -> ToolOutput:
    return _connection(args, apply=True)


def _allegro_audit(script_name: str) -> ToolOutput:
    allowed = {"place_check.py", "dangle_check.py", "silk_check.py"}
    if script_name not in allowed:
        raise ValueError(f"unsupported audit script: {script_name}")
    command = [sys.executable, str(BRIDGE_DIR / "allegro" / script_name)]
    if script_name == "place_check.py":
        command += ["--profile", "none"]
    completed = subprocess.run(
        command,
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    text = (completed.stdout + completed.stderr).strip()
    return ToolOutput(
        text or f"{script_name} produced no output",
        {"script": script_name, "exit_code": completed.returncode, "output": text},
        is_error=completed.returncode != 0,
    )


def t_allegro_placement_audit(_: dict) -> ToolOutput:
    return _allegro_audit("place_check.py")


def t_allegro_dangle_audit(_: dict) -> ToolOutput:
    return _allegro_audit("dangle_check.py")


def t_allegro_silkscreen_audit(_: dict) -> ToolOutput:
    return _allegro_audit("silk_check.py")


def t_run_workflow(args: dict) -> ToolOutput:
    name = args["workflow"]
    cap = SESSION.get()
    lines = cap.run_workflow(name)
    t = cap.triage(lines)
    head = f"{name}: {len(t['errors'])} ERROR, {len(t['warnings'])} WARN"
    note = ""
    if name == "bomScrubber":
        note = (
            "\n(note: bomScrubber emits a table rather than ERROR/WARN prefixes, "
            "so the counts above under-report it; read the full output.)"
        )
    selected, meta = _page(lines, args, default=200)
    data = {
        "workflow": name,
        "errors": t["errors"],
        "warnings": t["warnings"],
        "lines": selected,
        "pagination": meta,
    }
    return ToolOutput(head + note + "\n\n" + "\n".join(selected), data)


def t_set_part_property(args: dict) -> str:
    els = parse_tcl_list(
        SESSION.get().call(
            "::capBridge::setPartProp",
            [args["refdes"], args["property"], args["value"]],
        )
    )
    if els and els[0] == "ERROR":
        return f"ERROR: {' '.join(els[1:])}"
    # OK refdes prop old new
    return (
        f"Set {els[1]}.{els[2]}\n"
        f"  was : {els[3]!r}\n"
        f"  now : {els[4]!r}\n"
        "Geometry can be saved by capture_save_design, but Capture 17.4 does "
        "not persist this property edit through DboSession_SaveDesign. Use the "
        "Capture GUI File > Save/Close > Save to make it durable."
    )


def t_save_design(_: dict) -> str:
    els = parse_tcl_list(SESSION.get().call("::capBridge::saveDesign"))
    if els and els[0] == "ERROR":
        return f"ERROR: {' '.join(els[1:])}"
    return f"Saved design {els[2] if len(els) > 2 else ''}."


# --------------------------------------------------------------------------
# Allegro PCB Editor — separate bridge, separate lifecycle.
#
# Capture and Allegro are independent processes; either can be closed or
# restarted without the other. Each session connects lazily and resets on
# error rather than being held open and assumed healthy.
# --------------------------------------------------------------------------

class AllegroSession:
    def __init__(self) -> None:
        self._a: AllegroBridge | None = None

    def get(self) -> AllegroBridge:
        if self._a is None:
            a = AllegroBridge(timeout=120)
            a.ping()
            if QUERY_IL.exists():
                a.send('load("{}")'.format(str(QUERY_IL).replace("\\", "/")))
            self._a = a
        return self._a

    def reset(self) -> None:
        if self._a is not None:
            try:
                self._a.close()
            except Exception:
                pass
        self._a = None


ALLEGRO = AllegroSession()

#: Delimiters used by allegroQuery.il — neither character appears in Allegro
#: constraint names, layer names or refdes, so no escaping is needed.
_FS, _RS = "^", "|"


def _arecords(expr: str) -> list[list[str]]:
    """Run an Allegro query and split its delimited response into records."""
    raw = ALLEGRO.get().send(expr).strip('"')
    if raw.startswith("ERROR"):
        raise AllegroBridgeError(raw.replace(_FS, " "))
    if not raw:
        return []
    return [r.split(_FS) for r in raw.split(_RS)]


def _pad(row: list[str], n: int) -> list[str]:
    return row + [""] * (n - len(row))


def t_allegro_status(_: dict) -> str:
    recs = _arecords("aqBoard()")
    if not recs:
        return "No active board."
    f = _pad(recs[0], 7)
    labels = ("board", "active DRCs", "waived DRCs", "placed symbols",
              "symbol defs", "padstacks", "nets")
    return "\n".join(f"{k:15}: {v}" for k, v in zip(labels, f))


def t_allegro_drcs(args: dict) -> str:
    recs = _arecords("aqDrcs()")
    if not recs:
        return "No DRC violations."
    # Dedup on (constraint, location): a DRC marks EACH participating figure,
    # so one physical problem appears once per figure (max 2).
    seen: set = set()
    uniq: list[list[str]] = []
    by_name: dict[str, int] = {}
    for r in recs:
        p = _pad(r, 7)
        by_name[p[1]] = by_name.get(p[1], 0) + 1
        key = (p[1], p[5], p[6])
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    out = [f"{len(recs)} DRC records, {len(uniq)} distinct locations", "", "BY CONSTRAINT:"]
    out += [f"  {v:5}  {k}" for k, v in sorted(by_name.items(), key=lambda kv: -kv[1])]
    if args.get("detail"):
        out += ["", "DETAIL (deduplicated):"]
        out += [f"  {r[1]:32} exp {r[3]:>10}  act {r[4]:>10}  @ ({r[5]},{r[6]})"
                for r in uniq[:200]]
    return "\n".join(out)


def t_allegro_symbols(_: dict) -> str:
    recs = _arecords("aqSymbols()")
    if not recs:
        return "No placed symbols."
    out = [f"{len(recs)} placed symbols", "",
           f"{'REFDES':10} {'SYMBOL':30} {'X':>10} {'Y':>10} LAYER"]
    for r in recs:
        p = _pad(r, 6)
        out.append(f"{p[0] or '(none)':10} {p[1]:30} {p[2]:>10} {p[3]:>10} {p[4]}")
    return "\n".join(out)


def t_allegro_nets(_: dict) -> str:
    recs = _arecords("aqNets()")
    if not recs:
        return "No nets."
    rows = []
    for r in recs:
        p = _pad(r, 2)
        rows.append((p[0], int(p[1]) if p[1].isdigit() else 0))
    rows.sort(key=lambda x: -x[1])
    out = [f"{len(rows)} nets", ""]
    out += [f"  {n:26} {c:>4} pins" for n, c in rows]
    orphan = [n for n, c in rows if c == 0]
    single = [n for n, c in rows if c == 1]
    if orphan:
        out += ["", "ORPHANED (0 pins): " + ", ".join(orphan)]
    if single:
        out += ["SINGLE-NODE (1 pin): " + ", ".join(single)]
    return "\n".join(out)


def t_allegro_eval(args: dict) -> str:
    return ALLEGRO.get().send(args["expression"])


READ_TOOLS: dict[str, tuple[Callable[[dict], str | ToolOutput], str, dict]] = {
    "capture_status": (
        t_status,
        "Connection and active-design summary for the running OrCAD Capture session.",
        {"type": "object", "properties": {}},
    ),
    "capture_list_parts": (
        t_parts,
        "List placed parts with refdes, page, value, footprint and part number.",
        {
            "type": "object",
            "properties": {
                "page": {"type": "string", "description": "Optional page name filter."},
                **PAGING_SCHEMA,
            },
        },
    ),
    "capture_list_nets": (
        t_nets,
        "List every net with its pin count; flags orphaned and single-node nets.",
        {"type": "object", "properties": PAGING_SCHEMA},
    ),
    "capture_connectivity": (
        t_connectivity,
        "Netlist as net -> connected reference designators.",
        {
            "type": "object",
            "properties": {
                "net": {"type": "string", "description": "Optional single net name."},
                **PAGING_SCHEMA,
            },
        },
    ),
    "capture_part_properties": (
        t_part_properties,
        "All effective properties of one part. Use this to discover exact property "
        "names before writing — Capture uses spaces, e.g. 'PCB Footprint'.",
        {
            "type": "object",
            "properties": {"refdes": {"type": "string"}},
            "required": ["refdes"],
        },
    ),
    "capture_hanging_wires": (
        t_hanging_wires,
        "Find wire endpoints that connect to nothing. Locates the physical cause "
        "of orphaned (0-pin) nets, which cannot be found by name because "
        "page-level nets are unnamed.",
        {
            "type": "object",
            "properties": {"page": {"type": "string"}, **PAGING_SCHEMA},
        },
    ),
    "capture_list_pages": (
        t_pages,
        "List schematic pages with their owning schematic and placed-part count. Results are paginated.",
        {"type": "object", "properties": PAGING_SCHEMA},
    ),
    "capture_list_pins": (
        t_pins,
        "List placed part pins with page-absolute database coordinates. Filter by page or refdes before wiring.",
        {
            "type": "object",
            "properties": {
                "page": {"type": "string"},
                "refdes": {"type": "string"},
                **PAGING_SCHEMA,
            },
        },
    ),
    "capture_locate_net": (
        t_locate_net,
        "Locate a flattened net on schematic page wire geometry and count its wire segments per page.",
        {
            "type": "object",
            "properties": {"net": {"type": "string", "minLength": 1}},
            "required": ["net"],
        },
    ),
    "capture_list_library_packages": (
        t_library_packages,
        "List exact package names in an OLB already open in the Capture session; use before any placement workflow.",
        {
            "type": "object",
            "properties": {
                "library_path": {"type": "string", "minLength": 1},
                **PAGING_SCHEMA,
            },
            "required": ["library_path"],
        },
    ),
    "capture_list_open_libraries": (
        t_open_libraries,
        "List exact OLB paths registered in the current Capture session. Use these returned paths with the package tools; do not guess or normalize them.",
        {"type": "object", "properties": PAGING_SCHEMA},
    ),
    "capture_get_package_info": (
        t_package_info,
        "Read the exact device, designator and PCB footprint metadata for one Capture library package.",
        {
            "type": "object",
            "properties": {
                "library_path": {"type": "string", "minLength": 1},
                "package": {"type": "string", "minLength": 1},
            },
            "required": ["library_path", "package"],
        },
    ),
    "capture_preview_pin_connection": (
        t_preview_connection,
        "Resolve two part pins to absolute coordinates and verify they are on the same page without changing the design.",
        {
            "type": "object",
            "properties": {
                "refdes_a": {"type": "string"}, "pin_a": {"type": "string"},
                "refdes_b": {"type": "string"}, "pin_b": {"type": "string"},
            },
            "required": ["refdes_a", "pin_a", "refdes_b", "pin_b"],
        },
    ),
    "capture_engineering_audit": (
        t_engineering_audit,
        "Run a bounded schematic sign-off audit: BOM completeness, netlist readiness, high-speed naming, orphan nets and hanging endpoints.",
        {
            "type": "object",
            "properties": {
                "max_findings": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50}
            },
        },
    ),
    "allegro_status": (
        t_allegro_status,
        "Summary of the board open in Allegro PCB Editor: name, DRC counts, "
        "symbol/padstack/net counts.",
        {"type": "object", "properties": {}},
    ),
    "allegro_drcs": (
        t_allegro_drcs,
        "DRC violations grouped by constraint. Deduplicates the double-reporting "
        "caused by each violation marking both participating figures. Pass "
        "detail=true for per-violation locations.",
        {"type": "object", "properties": {"detail": {"type": "boolean"}}},
    ),
    "allegro_symbols": (
        t_allegro_symbols,
        "Placed footprint instances with refdes, symbol name and location.",
        {"type": "object", "properties": {}},
    ),
    "allegro_nets": (
        t_allegro_nets,
        "Board nets with pin counts; flags orphaned and single-node nets.",
        {"type": "object", "properties": {}},
    ),
    "allegro_placement_audit": (
        t_allegro_placement_audit,
        "Run the repository's geometry-only board placement audit (keep-in, overlap and basic placement checks).",
        {"type": "object", "properties": {}},
    ),
    "allegro_dangle_audit": (
        t_allegro_dangle_audit,
        "Run the repository's copper dangle audit against the live Allegro board.",
        {"type": "object", "properties": {}},
    ),
    "allegro_silkscreen_audit": (
        t_allegro_silkscreen_audit,
        "Run the read-only silkscreen clutter, pad-overlap and unlabeled-part audit.",
        {"type": "object", "properties": {}},
    ),
    "capture_run_workflow": (
        t_run_workflow,
        "Run a design-audit workflow and return its full report.",
        {
            "type": "object",
            "properties": {
                "workflow": {
                    "type": "string",
                    "enum": list(CaptureBridge.WORKFLOWS),
                },
                **PAGING_SCHEMA,
            },
            "required": ["workflow"],
        },
    ),
}

WRITE_TOOLS: dict[str, tuple[Callable[[dict], str | ToolOutput], str, dict]] = {
    "allegro_eval": (
        t_allegro_eval,
        "Evaluate an arbitrary single-line SKILL expression in Allegro. This "
        "escape hatch can mutate the board and is therefore available only "
        "with --allow-write.",
        {
            "type": "object",
            "properties": {"expression": {"type": "string", "minLength": 1}},
            "required": ["expression"],
        },
    ),
    "capture_set_part_property": (
        t_set_part_property,
        "Set a property on a part. Returns the previous value so the change can be "
        "undone. Does NOT save; call capture_save_design afterwards.",
        {
            "type": "object",
            "properties": {
                "refdes": {"type": "string"},
                "property": {"type": "string", "description": "Exact name, e.g. 'PCB Footprint'."},
                "value": {"type": "string"},
            },
            "required": ["refdes", "property", "value"],
        },
    ),
    "capture_save_design": (
        t_save_design,
        "Persist active-design geometry to disk. Capture 17.4 does not persist property edits through this API; use the GUI save flow for those. Back up first.",
        {"type": "object", "properties": {}},
    ),
    "capture_connect_pins": (
        t_connect_pins,
        "Create a database-level wire between two exact pins on the same page. Preview first with capture_preview_pin_connection; saving is separate.",
        {
            "type": "object",
            "properties": {
                "refdes_a": {"type": "string"}, "pin_a": {"type": "string"},
                "refdes_b": {"type": "string"}, "pin_b": {"type": "string"},
            },
            "required": ["refdes_a", "pin_a", "refdes_b", "pin_b"],
        },
    ),
}


def build_tools(allow_write: bool) -> dict:
    tools = dict(READ_TOOLS)
    if allow_write:
        tools.update(WRITE_TOOLS)
    return tools


# --------------------------------------------------------------------------
# JSON-RPC / MCP plumbing
# --------------------------------------------------------------------------

def send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def result(msg_id: Any, payload: dict) -> None:
    send({"jsonrpc": "2.0", "id": msg_id, "result": payload})


def error(msg_id: Any, code: int, message: str) -> None:
    send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})


def _validate_arguments(schema: dict, args: Any) -> None:
    """Small dependency-free validator for the JSON Schema subset used here."""

    if not isinstance(args, dict):
        raise ValueError("arguments must be a JSON object")
    properties = schema.get("properties", {})
    missing = [key for key in schema.get("required", []) if key not in args]
    if missing:
        raise ValueError("missing required argument(s): " + ", ".join(missing))
    unknown = sorted(set(args) - set(properties))
    if unknown:
        raise ValueError("unknown argument(s): " + ", ".join(unknown))
    python_types = {"string": str, "integer": int, "boolean": bool, "number": (int, float)}
    for key, value in args.items():
        spec = properties.get(key, {})
        expected = spec.get("type")
        if expected in python_types:
            wanted = python_types[expected]
            if expected == "integer" and isinstance(value, bool):
                ok = False
            else:
                ok = isinstance(value, wanted)
            if not ok:
                raise ValueError(f"{key} must be {expected}")
        if "enum" in spec and value not in spec["enum"]:
            raise ValueError(f"{key} must be one of: {', '.join(map(str, spec['enum']))}")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in spec and value < spec["minimum"]:
                raise ValueError(f"{key} must be >= {spec['minimum']}")
            if "maximum" in spec and value > spec["maximum"]:
                raise ValueError(f"{key} must be <= {spec['maximum']}")
        if isinstance(value, str) and len(value) < spec.get("minLength", 0):
            raise ValueError(f"{key} must not be empty")


def _annotations(name: str) -> dict[str, bool]:
    write = name in WRITE_TOOLS
    return {
        "readOnlyHint": not write,
        "destructiveHint": name in {"allegro_eval", "capture_save_design", "capture_connect_pins"},
        "idempotentHint": name not in {"allegro_eval", "capture_connect_pins"},
        "openWorldHint": False,
    }


def serve(allow_write: bool) -> int:
    tools = build_tools(allow_write)
    log(f"ready; {len(tools)} tools; write={'ENABLED' if allow_write else 'disabled'}")

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue

        method = msg.get("method")
        msg_id = msg.get("id")

        # Notifications carry no id and must not be answered.
        if msg_id is None and method and method.startswith("notifications/"):
            continue

        try:
            if method == "initialize":
                requested = (msg.get("params") or {}).get("protocolVersion")
                result(
                    msg_id,
                    {
                        "protocolVersion": requested or PROTOCOL_VERSION,
                        "capabilities": {"tools": {}},
                        "serverInfo": SERVER_INFO,
                    },
                )
            elif method == "ping":
                result(msg_id, {})
            elif method == "tools/list":
                result(
                    msg_id,
                    {
                        "tools": [
                            {
                                "name": n,
                                "description": d,
                                "inputSchema": s,
                                "outputSchema": {"type": "object", "additionalProperties": True},
                                "annotations": _annotations(n),
                            }
                            for n, (_, d, s) in tools.items()
                        ]
                    },
                )
            elif method == "tools/call":
                params = msg.get("params") or {}
                name = params.get("name")
                args = params.get("arguments") or {}
                entry = tools.get(name)
                if entry is None:
                    known = ", ".join(tools)
                    hint = ""
                    if not allow_write and name in WRITE_TOOLS:
                        hint = " (write tools require --allow-write)"
                    result(
                        msg_id,
                        {
                            "content": [{"type": "text", "text": f"Unknown tool {name!r}{hint}. Available: {known}"}],
                            "isError": True,
                        },
                    )
                else:
                    fn = entry[0]
                    structured = None
                    try:
                        _validate_arguments(entry[2], args)
                        output = fn(args)
                        if isinstance(output, ToolOutput):
                            text = output.text
                            structured = output.data
                            is_err = output.is_error
                        else:
                            text = output
                            structured = {"text": text}
                            is_err = text.startswith("ERROR")
                    except ValueError as exc:
                        text = f"Invalid arguments: {exc}"
                        structured = {"error": "invalid_arguments", "message": str(exc)}
                        is_err = True
                    except CaptureBridgeError as exc:
                        SESSION.reset()
                        text = (
                            f"Capture bridge error: {exc}\n\n"
                            "If Capture was restarted, the Communication Server must be "
                            "started again:\n"
                            "  package require capCommServer\n"
                            "  ::capCommServer::StartServer"
                        )
                        is_err = True
                    except AllegroBridgeError as exc:
                        ALLEGRO.reset()
                        text = (
                            f"Allegro bridge error: {exc}\n\n"
                            "If Allegro was restarted, the bridge must be started again "
                            "at the Skill> prompt:\n"
                            '  load(".../bridge/allegro/allegroBridge.il")\n'
                            "  abStart()"
                        )
                        is_err = True
                    except Exception as exc:  # noqa: BLE001
                        SESSION.reset()
                        log(traceback.format_exc())
                        text = f"Unexpected error: {exc!r}"
                        structured = None
                        is_err = True
                    payload: dict[str, Any] = {
                        "content": [{"type": "text", "text": text}],
                        "isError": is_err,
                    }
                    if structured is not None:
                        payload["structuredContent"] = structured
                    result(
                        msg_id,
                        payload,
                    )
            elif msg_id is not None:
                error(msg_id, -32601, f"Method not found: {method}")
        except Exception as exc:  # noqa: BLE001
            log(traceback.format_exc())
            if msg_id is not None:
                error(msg_id, -32603, f"Internal error: {exc!r}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--allow-write",
        action="store_true",
        help="Expose mutation tools (SKILL eval, property/wire edits, save). Off by default.",
    )
    ns = ap.parse_args()
    return serve(ns.allow_write)


if __name__ == "__main__":
    raise SystemExit(main())
