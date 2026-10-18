"""Pure current-registry attribution and disjoint, same-domain energy groups.

Callers supply one verified interval and one captured registry snapshot. This
module makes no historical room claim and does not read HA, price or allocate
missing consumption. HA IO and revision invalidation belong to the coordinator.
"""
from decimal import Decimal
from math import isfinite

MAX_SOURCES = 512


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _label(value):
    return value if isinstance(value, str) and len(value) <= 256 else None


def resolve_membership(source_ids, registries, *, captured_at, revision):
    """Detach only requested entities: entity area overrides the device area.

    An unavailable floor registry is an explicit capability fallback. External
    statistics and deleted registry references remain unassigned, never guessed.
    """
    if (not isinstance(source_ids, list) or len(source_ids) > MAX_SOURCES
            or any(not isinstance(sid, str) or not sid for sid in source_ids)
            or len(set(source_ids)) != len(source_ids)):
        raise ValueError("invalid_source_ids")
    registries = _mapping(registries)
    entities, devices, areas, floors = (_mapping(registries.get(key)) for key in
                                       ("entities", "devices", "areas", "floors"))
    floor_supported = isinstance(registries.get("floors"), dict)
    sources = {}
    for sid in source_ids:
        entity = _mapping(entities.get(sid))
        device = _mapping(devices.get(entity.get("device_id")))
        area_id = entity.get("area_id") or device.get("area_id")
        attribution = "entity" if entity.get("area_id") else "device" if area_id else "unassigned"
        area = _mapping(areas.get(area_id)) if isinstance(area_id, str) else {}
        if not area:
            area_id, attribution = None, "unassigned"
        floor_id = area.get("floor_id") if floor_supported else None
        floor = _mapping(floors.get(floor_id)) if isinstance(floor_id, str) else {}
        if not floor:
            floor_id = None
        sources[sid] = {"entity_id": sid if entity else None,
                        "name": _label(entity.get("name") or entity.get("original_name")),
                        "area_id": area_id, "area_name": _label(area.get("name")),
                        "floor_id": floor_id, "floor_name": _label(floor.get("name")),
                        "attribution": attribution}
    return {"sources": sources, "revision": revision, "captured_at": captured_at,
            "membership_mode": "current_snapshot", "floor_supported": floor_supported}


def _energy(value):
    if type(value) in (int, float) and isfinite(value) and 0 <= value <= 1e12:
        return Decimal(str(value))
    return None


def build_interval_breakdown(grid_kwh, consumers, membership):
    """Partition measured parents into direct-child totals and parent residuals.

    Every row must explicitly share the grid_import domain. A site-consumption
    meter behind PV/battery cannot prove a grid-import partition. A missing or
    inconsistent value remains unknown; it is never distributed proportionally.
    """
    empty = {"status": "unknown", "issues": [], "devices": {}, "areas": {}, "floors": {},
             "unassigned": {"kwh": None}, "unmeasured": {"kwh": None}, "partition_kwh": None,
             "membership_as_of": membership.get("captured_at"),
             "registry_revision": membership.get("revision"), "membership_mode": "current_snapshot"}
    if not isinstance(consumers, list) or len(consumers) > MAX_SOURCES:
        return {**empty, "issues": [{"reason": "invalid_consumers"}]}
    nodes, children = {}, {}
    for row in consumers:
        sid = row.get("stat_consumption") if isinstance(row, dict) else None
        parent = row.get("included_in_stat") if isinstance(row, dict) else None
        if (not isinstance(sid, str) or not sid or sid in nodes
                or (parent is not None and (not isinstance(parent, str) or not parent))):
            return {**empty, "issues": [{"reason": "duplicate_or_invalid_source"}]}
        if row.get("domain") != "grid_import":
            return {**empty, "issues": [{"reason": "domain_mismatch", "source_id": sid}]}
        nodes[sid] = {"parent": parent, "value": _energy(row.get("kwh"))}
        children.setdefault(parent, []).append(sid)
    # Bounded iterative walks avoid recursion depth and reject ambiguous trees.
    for sid, node in nodes.items():
        if node["parent"] is not None and node["parent"] not in nodes:
            return {**empty, "issues": [{"reason": "missing_parent", "source_id": sid}]}
        seen, current = set(), sid
        while current is not None:
            if current in seen:
                return {**empty, "issues": [{"reason": "hierarchy_cycle", "source_id": sid}]}
            seen.add(current)
            current = nodes[current]["parent"]
    issues, devices, area_values, floor_values, unassigned = [], {}, {}, {}, []
    memberships = _mapping(membership.get("sources"))
    for sid, node in nodes.items():
        value = node["value"]
        descendants = [nodes[child]["value"] for child in children.get(sid, [])]
        residual = None if value is None or any(v is None for v in descendants) else value - sum(descendants, Decimal(0))
        if residual is not None and residual < 0:
            issues.append({"reason": "children_exceed_parent", "source_id": sid})
            residual = None
        if residual is None:
            issues.append({"reason": "unknown_attribution", "source_id": sid})
        assignment = _mapping(memberships.get(sid))
        area_id, floor_id = assignment.get("area_id"), assignment.get("floor_id")
        devices[sid] = {"kwh": None if residual is None else float(residual),
                        "measured_kwh": None if value is None else float(value),
                        "attribution": "parent_residual" if descendants else "measured",
                        "area_id": area_id, "floor_id": floor_id, "parent_id": node["parent"]}
        if area_id is None:
            unassigned.append(residual)
        else:
            area_values.setdefault(area_id, []).append(residual)
        floor_values.setdefault(floor_id or "__no_floor__", []).append(residual)

    def group(values):
        known = sum((v for v in values if v is not None), Decimal(0))
        return {"kwh": float(known) if all(v is not None for v in values) else None,
                "measured_kwh": float(known), "complete": all(v is not None for v in values)}

    roots = [nodes[sid]["value"] for sid in children.get(None, [])]
    root_total = sum(roots, Decimal(0)) if all(v is not None for v in roots) else None
    grid = _energy(grid_kwh)
    unmeasured = None
    if grid is not None and root_total is not None and not any(i["reason"] == "children_exceed_parent" for i in issues):
        if root_total > grid:
            issues.append({"reason": "consumption_exceeds_grid_import"})
        else:
            unmeasured = grid - root_total
    else:
        issues.append({"reason": "unknown_measurement"})
    ready = not issues and unmeasured is not None
    return {**empty, "status": "ready" if ready else "partial", "issues": issues,
            "devices": devices, "areas": {key: group(values) for key, values in area_values.items()},
            "floors": {key: group(values) for key, values in floor_values.items()},
            "unassigned": group(unassigned),
            "unmeasured": {"kwh": None if unmeasured is None else float(unmeasured)},
            "partition_kwh": float(grid) if ready else None}
