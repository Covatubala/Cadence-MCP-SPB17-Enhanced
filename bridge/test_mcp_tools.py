#!/usr/bin/env python3
"""Offline contract smoke test for every published MCP tool.

This test intentionally needs neither Capture nor Allegro.  It replaces both
bridges with protocol-faithful fakes, invokes every tool implementation through
its public input shape, and checks that each one produces a non-error result.
Live application coverage is documented separately in TEST_REPORT.md.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bridge.cadence_mcp as m  # noqa: E402


class FakeCapture:
    WORKFLOWS = m.CaptureBridge.WORKFLOWS

    def tcl_version(self):
        return "8.6.5"

    def dbo_command_count(self):
        return 4185

    def has_active_design(self):
        return True

    def parts(self):
        return [{
            "refdes": "R1", "schematic": "SCHEMATIC1", "page": "PAGE1",
            "value": "10k", "footprint": "R0402", "part_number": "PN-1",
        }]

    def nets(self):
        return [{"net": "NET1", "pin_count": 2}]

    def connectivity(self):
        return [{"net": "NET1", "pin_count": 2, "refdes": ["R1", "R2"]}]

    def call(self, proc, args=()):
        replies = {
            "::capBridge::designInfo": "OK SCHEMATIC1 1 1 1",
            "::capBridge::partProps": "OK {Value 10k} {{PCB Footprint} R0402}",
            "::capBridge::hangingWires": "OK {PAGE1 10 20 start}",
            "::capBridge::pages": "OK {SCHEMATIC1 PAGE1 1}",
            "::capBridge::pins": "OK {SCHEMATIC1 PAGE1 R1 1 10 20}",
            "::capBridge::locateNet": "OK {PAGE1 2}",
            "::capBridge::libPackages": "OK RESISTOR CAP_NP",
            "::capBridge::openLibs": "OK V:/mock.olb",
            "::capBridge::pkgInfo": "OK device RESISTOR designator R footprint R0402",
            "::capBridge::setPartProp": "OK R1 Value 10k 10k",
            "::capBridge::saveDesign": "OK saved mock.dsn",
        }
        if proc == "::capBridge::placeWireBetweenPins":
            apply = bool(args) and str(args[-1]).lower() == "true"
            action = "created wire mock-wire" if apply else "preview"
            return f"OK {action} page PAGE1 from R1.1 10,20 to R2.1 30,20"
        if proc not in replies:
            raise AssertionError(f"unhandled fake Capture proc: {proc}")
        return replies[proc]

    def run_workflow(self, name):
        return [
            f"{name} report",
            "WARN sample warning" if name != "bomScrubber" else "R1 [Page: PAGE1] Missing: Part Number",
            "SUMMARY:  0 ERROR(s)   1 WARNING(s)",
        ]

    @staticmethod
    def triage(lines):
        return {
            "errors": [x.strip() for x in lines if x.strip().startswith("ERROR")],
            "warnings": [x.strip() for x in lines if x.strip().startswith("WARN")],
        }


class FakeSession:
    def __init__(self):
        self.bridge = FakeCapture()

    def get(self):
        return self.bridge

    def reset(self):
        return None


class FakeAllegro:
    def send(self, expression):
        replies = {
            "aqBoard()": "mock.brd^0^0^2^2^2^2",
            "aqDrcs()": "NET SPACING^Line to Line^TOP^12 MIL^12 MIL^10^20",
            "aqSymbols()": "R1^R0402^10^20^TOP^0",
            "aqNets()": "NET1^2|NET2^1",
        }
        return replies.get(expression, "2")

    def close(self):
        return None


class FakeAllegroSession:
    def __init__(self):
        self.bridge = FakeAllegro()

    def get(self):
        return self.bridge

    def reset(self):
        return None


ARGS = {
    "capture_status": {},
    "capture_list_parts": {"limit": 10},
    "capture_list_nets": {"limit": 10},
    "capture_connectivity": {"net": "NET1", "limit": 10},
    "capture_part_properties": {"refdes": "R1"},
    "capture_hanging_wires": {"page": "PAGE1", "limit": 10},
    "capture_list_pages": {"limit": 10},
    "capture_list_pins": {"refdes": "R1", "limit": 10},
    "capture_locate_net": {"net": "NET1"},
    "capture_list_library_packages": {"library_path": "mock.olb", "limit": 10},
    "capture_list_open_libraries": {"limit": 10},
    "capture_get_package_info": {"library_path": "mock.olb", "package": "RESISTOR"},
    "capture_preview_pin_connection": {
        "refdes_a": "R1", "pin_a": "1", "refdes_b": "R2", "pin_b": "1",
    },
    "capture_engineering_audit": {"max_findings": 5},
    "capture_run_workflow": {"workflow": "preNetlistCheck", "limit": 10},
    "allegro_status": {},
    "allegro_drcs": {"detail": True},
    "allegro_symbols": {},
    "allegro_nets": {},
    "allegro_placement_audit": {},
    "allegro_dangle_audit": {},
    "allegro_silkscreen_audit": {},
    "allegro_eval": {"expression": "1+1"},
    "capture_set_part_property": {"refdes": "R1", "property": "Value", "value": "10k"},
    "capture_save_design": {},
    "capture_connect_pins": {
        "refdes_a": "R1", "pin_a": "1", "refdes_b": "R2", "pin_b": "1",
    },
}


def fake_run(*_args, **_kwargs):
    return subprocess.CompletedProcess(["fake"], 0, "audit passed\n", "")


def main() -> int:
    original_session, original_allegro, original_run = m.SESSION, m.ALLEGRO, m.subprocess.run
    m.SESSION, m.ALLEGRO, m.subprocess.run = FakeSession(), FakeAllegroSession(), fake_run
    failures = []
    try:
        tools = m.build_tools(True)
        if set(tools) != set(ARGS):
            missing = sorted(set(tools) - set(ARGS))
            stale = sorted(set(ARGS) - set(tools))
            raise AssertionError(f"sample argument map mismatch; missing={missing}, stale={stale}")
        for name, (fn, _description, schema) in tools.items():
            try:
                args = ARGS[name]
                m._validate_arguments(schema, args)
                output = fn(args)
                if isinstance(output, m.ToolOutput):
                    assert not output.is_error, output.text
                    assert isinstance(output.data, dict)
                else:
                    assert isinstance(output, str) and not output.startswith("ERROR")
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001 - aggregate all tools
                failures.append((name, repr(exc)))
                print(f"FAIL {name}: {exc!r}")
    finally:
        m.SESSION, m.ALLEGRO, m.subprocess.run = original_session, original_allegro, original_run

    print(f"\n{len(ARGS) - len(failures)}/{len(ARGS)} tool contracts passed")
    if failures:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
