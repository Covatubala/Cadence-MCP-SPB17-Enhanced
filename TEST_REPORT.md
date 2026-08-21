# MCP verification report

Tested on 2026-08-21 against OrCAD Capture CIS 17.4 and Python 3.11.

## Summary

- Offline regression suite: **passed**.
- MCP contract test: **26/26 tools passed**.
- Live Capture read/planning calls: **14 passed**, one library-detail call was
  unavailable because the active session exposed no package-bearing OLB.
- Live design mutation: intentionally not applied to the user's production
  schematic. All three Capture write tools passed the complete fake-bridge
  call path; pin resolution and coordinates were additionally verified live by
  `capture_preview_pin_connection`.
- Live Allegro board test: unavailable because Allegro PCB Editor and a `.brd`
  design were not open. All eight Allegro tools passed their MCP contract test;
  their no-listener paths returned actionable startup guidance.

The live Capture design reported Tcl 8.6.5, 4,185 `Dbo*` commands, 13 pages,
605 parts and 373 nets. The bridge listener was bound only to `127.0.0.1`.

## Tool matrix

| Tool | Offline contract | Live application result |
|---|---:|---|
| `capture_status` | Pass | Pass |
| `capture_list_parts` | Pass | Pass |
| `capture_list_nets` | Pass | Pass |
| `capture_connectivity` | Pass | Pass |
| `capture_part_properties` | Pass | Pass |
| `capture_hanging_wires` | Pass | Pass |
| `capture_list_pages` | Pass | Pass |
| `capture_list_pins` | Pass | Pass |
| `capture_locate_net` | Pass | Pass |
| `capture_list_open_libraries` | Pass | Pass; exact Capture path returned |
| `capture_list_library_packages` | Pass | Pass; current `CAPSYM.OLB` returned an empty package set |
| `capture_get_package_info` | Pass | Environment-blocked; no package was exposed by the active OLB |
| `capture_preview_pin_connection` | Pass | Pass; resolved `U35.1` and `U36.1` without writing |
| `capture_run_workflow` | Pass | Pass; `preNetlistCheck` returned structured findings |
| `capture_engineering_audit` | Pass | Pass; all four packaged workflows completed |
| `capture_set_part_property` | Pass | Not applied to production design |
| `capture_save_design` | Pass | Not applied to production design |
| `capture_connect_pins` | Pass | Not applied; its live non-mutating preview path passed |
| `allegro_status` | Pass | Environment-blocked; no listener on port 9030 |
| `allegro_drcs` | Pass | Environment-blocked; actionable startup error verified |
| `allegro_symbols` | Pass | Environment-blocked; actionable startup error verified |
| `allegro_nets` | Pass | Environment-blocked; actionable startup error verified |
| `allegro_placement_audit` | Pass | Environment-blocked; failed result is returned as MCP error content |
| `allegro_dangle_audit` | Pass | Environment-blocked; failed result is returned as MCP error content |
| `allegro_silkscreen_audit` | Pass | Environment-blocked; failed result is returned as MCP error content |
| `allegro_eval` | Pass | Environment-blocked; no listener; remains opt-in because SKILL can mutate |

## Regression commands

```powershell
python -m py_compile bridge/cadence_mcp.py bridge/test_bridge.py bridge/test_mcp_tools.py
python bridge/test_bridge.py
python bridge/test_mcp_tools.py
```

The suite verifies Tcl quoting and parsing, round trips, request guards,
Allegro record parsing, JSON Schema validation, pagination and every MCP tool
handler. No proprietary design files or Cadence-generated artifacts are
included in the repository.

## Live-test safety notes

An initial library lookup revealed that resolving a mapped `V:` path to a UNC
path can change the exact spelling Capture uses as its session key. Passing
that transformed path to Capture's direct library getter caused Capture 17.4
to exit. The implementation was changed to preserve mapped-drive spelling and
to enumerate the session's open libraries before exact matching. The repaired
path was then retested live: open-library enumeration and package listing both
completed without destabilizing Capture.

The earlier process was restarted and the recovered design remained available.
No property, wire, component, or board mutation was made during verification.
