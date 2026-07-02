#!/usr/bin/env python3
"""Partition a binary AIGER .aig file and report cross-partition cut variables.

The script converts AIG variables to an undirected METIS graph:
  - Primary-output driver variables are excluded from the METIS graph.
  - Remaining AIG variables are compactly mapped to METIS vertices 1..N.
  - Each AND edge fanin -> gate becomes an undirected graph edge.
  - Inversion bits are ignored for partitioning, but kept in the JSON report.

It prefers gpmetis when available. If gpmetis is not available, it falls back to
a deterministic two-way Kernighan-Lin refinement so the workflow still produces
cut variables for small/medium AIGs without extra dependencies.
"""

from __future__ import annotations

import argparse
import html
import json
import shutil
import subprocess
import sys
from collections import Counter, defaultdict, deque
from pathlib import Path


class AigParseError(ValueError):
    pass


def read_ascii_line(data: bytes, pos: int) -> tuple[str, int]:
    end = data.find(b"\n", pos)
    if end < 0:
        raise AigParseError("unexpected EOF while reading ASCII section")
    return data[pos:end].decode("ascii").strip(), end + 1


def read_varint(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise AigParseError("unexpected EOF while reading binary AND delta")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, pos
        shift += 7
        if shift > 63:
            raise AigParseError("AIGER varint is too large")


def parse_aig(path: Path) -> dict:
    data = path.read_bytes()
    header_line, pos = read_ascii_line(data, 0)
    fields = header_line.split()
    if len(fields) < 6 or fields[0] != "aig":
        raise AigParseError(f"{path} is not a binary AIGER file with an 'aig' header")

    try:
        nums = [int(x) for x in fields[1:]]
    except ValueError as exc:
        raise AigParseError(f"invalid AIGER header: {header_line}") from exc

    maxvar, n_inputs, n_latches, n_outputs, n_ands = nums[:5]
    n_bad = nums[5] if len(nums) > 5 else 0
    n_constraints = nums[6] if len(nums) > 6 else 0
    n_justice = nums[7] if len(nums) > 7 else 0
    n_fairness = nums[8] if len(nums) > 8 else 0

    latches = []
    for latch_var in range(n_inputs + 1, n_inputs + n_latches + 1):
        line, pos = read_ascii_line(data, pos)
        values = line.split()
        if not values:
            raise AigParseError("empty latch line")
        latches.append({"var": latch_var, "next_lit": int(values[0])})

    outputs = []
    for index in range(n_outputs):
        line, pos = read_ascii_line(data, pos)
        outputs.append({"index": index, "lit": int(line.split()[0])})

    bad = []
    for index in range(n_bad):
        line, pos = read_ascii_line(data, pos)
        bad.append({"index": index, "lit": int(line.split()[0])})

    constraints = []
    for index in range(n_constraints):
        line, pos = read_ascii_line(data, pos)
        constraints.append({"index": index, "lit": int(line.split()[0])})

    justice = []
    for index in range(n_justice):
        line, pos = read_ascii_line(data, pos)
        size = int(line.split()[0])
        lits = []
        for _ in range(size):
            lit_line, pos = read_ascii_line(data, pos)
            lits.append(int(lit_line.split()[0]))
        justice.append({"index": index, "lits": lits})

    fairness = []
    for index in range(n_fairness):
        line, pos = read_ascii_line(data, pos)
        fairness.append({"index": index, "lit": int(line.split()[0])})

    ands = []
    first_and_var = n_inputs + n_latches + 1
    for offset in range(n_ands):
        lhs_lit = 2 * (first_and_var + offset)
        delta0, pos = read_varint(data, pos)
        delta1, pos = read_varint(data, pos)
        rhs0_lit = lhs_lit - delta0
        rhs1_lit = rhs0_lit - delta1
        if rhs0_lit < 0 or rhs1_lit < 0:
            raise AigParseError("decoded negative AND fanin literal")
        ands.append(
            {
                "var": lhs_lit >> 1,
                "lhs_lit": lhs_lit,
                "rhs0_lit": rhs0_lit,
                "rhs1_lit": rhs1_lit,
            }
        )

    return {
        "path": str(path),
        "maxvar": maxvar,
        "n_inputs": n_inputs,
        "n_latches": n_latches,
        "n_outputs": n_outputs,
        "n_ands": n_ands,
        "latches": latches,
        "outputs": outputs,
        "bad": bad,
        "constraints": constraints,
        "justice": justice,
        "fairness": fairness,
        "ands": ands,
    }


def lit_var(lit: int) -> int:
    return lit >> 1


def lit_inverted(lit: int) -> bool:
    return bool(lit & 1)


def output_driver_variables(aig: dict) -> list[int]:
    return sorted({lit_var(output["lit"]) for output in aig["outputs"] if lit_var(output["lit"]) > 0})


def collect_directed_edges(aig: dict) -> list[dict]:
    edges = []

    def append_edge(src_lit: int, dst_var: int, dst_lit: int, kind: str) -> None:
        edges.append(
            {
                "src_var": lit_var(src_lit),
                "dst_var": dst_var,
                "src_lit": src_lit,
                "dst_lit": dst_lit,
                "src_inverted": lit_inverted(src_lit),
                "kind": kind,
            }
        )

    for gate in aig["ands"]:
        append_edge(gate["rhs0_lit"], gate["var"], gate["lhs_lit"], "and_rhs0")
        append_edge(gate["rhs1_lit"], gate["var"], gate["lhs_lit"], "and_rhs1")

    for latch in aig["latches"]:
        append_edge(latch["next_lit"], latch["var"], 2 * latch["var"], "latch_next")

    return edges


def build_graph(aig: dict) -> tuple[list[set[int]], list[dict], list[int], dict[int, int], list[int]]:
    maxvar = aig["maxvar"]
    excluded_output_variables = output_driver_variables(aig)
    excluded = set(excluded_output_variables)
    metis_to_aig = [0] + [aig_var for aig_var in range(1, maxvar + 1) if aig_var not in excluded]
    aig_to_metis = {aig_var: metis_var for metis_var, aig_var in enumerate(metis_to_aig) if metis_var > 0}
    neighbors = [set() for _ in range(len(metis_to_aig))]
    directed_edges = []

    def add_directed_edge(edge: dict) -> None:
        src_var = edge["src_var"]
        dst_var = edge["dst_var"]
        if src_var <= 0 or dst_var <= 0 or src_var == dst_var:
            return
        src_metis = aig_to_metis.get(src_var)
        dst_metis = aig_to_metis.get(dst_var)
        if src_metis is None or dst_metis is None:
            return
        neighbors[src_metis].add(dst_metis)
        neighbors[dst_metis].add(src_metis)
        directed_edges.append(edge)

    for edge in collect_directed_edges(aig):
        add_directed_edge(edge)

    return neighbors, directed_edges, metis_to_aig, aig_to_metis, excluded_output_variables


def write_metis_graph(path: Path, neighbors: list[set[int]]) -> int:
    edge_count = sum(len(ns) for ns in neighbors[1:]) // 2
    with path.open("w", encoding="ascii") as handle:
        handle.write(f"{len(neighbors) - 1} {edge_count}\n")
        for vertex in range(1, len(neighbors)):
            handle.write(" ".join(str(n) for n in sorted(neighbors[vertex])))
            handle.write("\n")
    return edge_count


def write_vertex_map(path: Path, metis_to_aig: list[int]) -> None:
    with path.open("w", encoding="ascii") as handle:
        handle.write("metis_vertex\taig_variable\n")
        for metis_vertex, aig_variable in enumerate(metis_to_aig[1:], start=1):
            handle.write(f"{metis_vertex}\t{aig_variable}\n")


def compute_visualization_layout(aig: dict, original_edges: list[dict]) -> tuple[dict[object, tuple[float, float]], dict[int, int]]:
    levels = {aig_var: 0 for aig_var in range(1, aig["maxvar"] + 1)}
    for gate in aig["ands"]:
        fanin_levels = [levels.get(lit_var(gate[key]), 0) for key in ("rhs0_lit", "rhs1_lit")]
        levels[gate["var"]] = max(fanin_levels, default=0) + 1

    needs_constant = any(edge["src_var"] == 0 for edge in original_edges) or any(
        lit_var(output["lit"]) == 0 for output in aig["outputs"]
    )
    layers: dict[int, list[object]] = defaultdict(list)
    for aig_var in range(1, aig["maxvar"] + 1):
        layers[levels[aig_var]].append(aig_var)
    if needs_constant:
        layers[0].insert(0, "const:0")

    output_level = max(levels.values(), default=0) + 1
    for output in aig["outputs"]:
        layers[output_level].append(f"po:{output['index']}")

    parents: dict[object, list[object]] = defaultdict(list)
    children: dict[object, list[object]] = defaultdict(list)
    for edge in original_edges:
        src: object = edge["src_var"] if edge["src_var"] > 0 else "const:0"
        dst: object = edge["dst_var"]
        if src in levels or src == "const:0":
            parents[dst].append(src)
            children[src].append(dst)
    for output in aig["outputs"]:
        src_var = lit_var(output["lit"])
        src: object = src_var if src_var > 0 else "const:0"
        dst = f"po:{output['index']}"
        parents[dst].append(src)
        children[src].append(dst)

    def stable_token(node_id: object) -> tuple[int, object]:
        return (0, node_id) if isinstance(node_id, int) else (1, str(node_id))

    for layer in layers.values():
        layer.sort(key=stable_token)

    def normalized_positions() -> dict[object, float]:
        positions = {}
        for layer in layers.values():
            denominator = max(len(layer) - 1, 1)
            positions.update({node_id: index / denominator for index, node_id in enumerate(layer)})
        return positions

    for _ in range(4):
        positions = normalized_positions()
        for level in sorted(layers):
            previous = {node_id: index for index, node_id in enumerate(layers[level])}
            layers[level].sort(
                key=lambda node_id: (
                    sum(positions[parent] for parent in parents[node_id] if parent in positions)
                    / max(sum(1 for parent in parents[node_id] if parent in positions), 1)
                    if any(parent in positions for parent in parents[node_id])
                    else previous[node_id],
                    stable_token(node_id),
                )
            )

        positions = normalized_positions()
        for level in sorted(layers, reverse=True):
            previous = {node_id: index for index, node_id in enumerate(layers[level])}
            layers[level].sort(
                key=lambda node_id: (
                    sum(positions[child] for child in children[node_id] if child in positions)
                    / max(sum(1 for child in children[node_id] if child in positions), 1)
                    if any(child in positions for child in children[node_id])
                    else previous[node_id],
                    stable_token(node_id),
                )
            )

    x_spacing = 180.0
    y_spacing = 30.0
    margin = 90.0
    widest_layer = max((len(layer) for layer in layers.values()), default=1)
    world_height = max(600.0, (widest_layer - 1) * y_spacing + 2 * margin)
    coordinates: dict[object, tuple[float, float]] = {}
    for level, layer in layers.items():
        layer_height = max(len(layer) - 1, 0) * y_spacing
        start_y = (world_height - layer_height) / 2
        for index, node_id in enumerate(layer):
            coordinates[node_id] = (margin + level * x_spacing, start_y + index * y_spacing)

    return coordinates, levels


def compute_partition_visualization_layout(nodes: list[dict]) -> dict | None:
    partition_ids = sorted(
        {
            int(node["partition"])
            for node in nodes
            if node.get("partition") is not None
        }
    )
    if partition_ids != [0, 1]:
        return None

    grouped = {
        partition: [node for node in nodes if node.get("partition") == partition]
        for partition in partition_ids
    }
    neutral = [node for node in nodes if node.get("partition") is None]

    def source_bounds(group: list[dict]) -> tuple[float, float, float, float]:
        return (
            min(node["x"] for node in group),
            max(node["x"] for node in group),
            min(node["y"] for node in group),
            max(node["y"] for node in group),
        )

    grouped_bounds = {partition: source_bounds(grouped[partition]) for partition in partition_ids}
    content_width = max(max_x - min_x for min_x, max_x, _min_y, _max_y in grouped_bounds.values())
    content_height = max(max_y - min_y for _min_x, _max_x, min_y, max_y in grouped_bounds.values())
    horizontal_padding = 90.0
    header_padding = 360.0
    bottom_padding = 90.0
    margin = 90.0
    group_gap = 240.0
    region_width = max(900.0, content_width + 2 * horizontal_padding)
    region_height = max(990.0, content_height + header_padding + bottom_padding)
    group_top = margin

    groups = []
    for index, partition in enumerate(partition_ids):
        left = margin + index * (region_width + group_gap)
        min_x, max_x, min_y, max_y = grouped_bounds[partition]
        x_offset = left + horizontal_padding - min_x + (content_width - (max_x - min_x)) / 2
        y_offset = group_top + header_padding - min_y + (content_height - (max_y - min_y)) / 2
        for node in grouped[partition]:
            node["partitionX"] = round(node["x"] + x_offset, 3)
            node["partitionY"] = round(node["y"] + y_offset, 3)
        groups.append(
            {
                "partition": partition,
                "label": f"P{partition}",
                "nodeCount": len(grouped[partition]),
                "minX": round(left, 3),
                "maxX": round(left + region_width, 3),
                "minY": round(group_top, 3),
                "maxY": round(group_top + region_height, 3),
            }
        )

    neutral_region = None
    if neutral:
        neutral_top = group_top + region_height + 150.0
        neutral_horizontal_padding = 70.0
        neutral_header_padding = 300.0
        neutral_bottom_padding = 70.0
        neutral_min_x, neutral_max_x, neutral_min_y, neutral_max_y = source_bounds(neutral)
        neutral_source_width = max(neutral_max_x - neutral_min_x, 1.0)
        neutral_source_height = max(neutral_max_y - neutral_min_y, 1.0)
        total_width = region_width * 2 + group_gap
        neutral_width = min(total_width, max(700.0, neutral_source_width + 2 * neutral_horizontal_padding))
        neutral_left = margin + (total_width - neutral_width) / 2
        neutral_height = max(
            440.0,
            neutral_source_height + neutral_header_padding + neutral_bottom_padding,
        )
        for node in neutral:
            x_ratio = (node["x"] - neutral_min_x) / neutral_source_width
            y_ratio = (node["y"] - neutral_min_y) / neutral_source_height
            node["partitionX"] = round(
                neutral_left
                + neutral_horizontal_padding
                + x_ratio * (neutral_width - 2 * neutral_horizontal_padding),
                3,
            )
            node["partitionY"] = round(
                neutral_top
                + neutral_header_padding
                + y_ratio * (neutral_height - neutral_header_padding - neutral_bottom_padding),
                3,
            )
        neutral_region = {
            "label": "未参与划分 / 输出",
            "nodeCount": len(neutral),
            "minX": round(neutral_left, 3),
            "maxX": round(neutral_left + neutral_width, 3),
            "minY": round(neutral_top, 3),
            "maxY": round(neutral_top + neutral_height, 3),
        }

    return {
        "enabled": True,
        "partitionIds": partition_ids,
        "groups": groups,
        "neutralGroup": neutral_region,
    }


def build_visualization_data(
    aig: dict,
    part: list[int],
    aig_to_metis: dict[int, int],
    report: dict,
) -> dict:
    original_edges = collect_directed_edges(aig)
    coordinates, levels = compute_visualization_layout(aig, original_edges)
    cut_variables = set(report["cut_variables"])
    excluded_variables = set(report["excluded_output_variables"])
    cut_edge_keys = {(edge["src_var"], edge["dst_var"], edge["kind"]) for edge in report["cut_edges"]}
    and_variables = {gate["var"] for gate in aig["ands"]}
    first_latch = aig["n_inputs"] + 1
    last_latch = aig["n_inputs"] + aig["n_latches"]

    nodes = []
    for aig_var in range(1, aig["maxvar"] + 1):
        if aig_var <= aig["n_inputs"]:
            node_type = "input"
        elif first_latch <= aig_var <= last_latch:
            node_type = "latch"
        elif aig_var in and_variables:
            node_type = "and"
        else:
            node_type = "variable"
        metis_vertex = aig_to_metis.get(aig_var)
        x, y = coordinates[aig_var]
        nodes.append(
            {
                "id": aig_var,
                "label": str(aig_var),
                "type": node_type,
                "partition": part[metis_vertex] if metis_vertex is not None else None,
                "isCutVariable": aig_var in cut_variables,
                "excluded": aig_var in excluded_variables,
                "level": levels[aig_var],
                "x": round(x, 3),
                "y": round(y, 3),
            }
        )

    edge_data = []
    needs_constant = False
    for index, edge in enumerate(original_edges):
        source: object = edge["src_var"]
        if source == 0:
            source = "const:0"
            needs_constant = True
        edge_data.append(
            {
                "id": f"logic:{index}",
                "source": source,
                "target": edge["dst_var"],
                "kind": edge["kind"],
                "inverted": edge["src_inverted"],
                "srcLit": edge["src_lit"],
                "dstLit": edge["dst_lit"],
                "isCut": (edge["src_var"], edge["dst_var"], edge["kind"]) in cut_edge_keys,
                "isOutput": False,
            }
        )

    output_data = []
    for output in aig["outputs"]:
        driver_var = lit_var(output["lit"])
        source: object = driver_var
        if source == 0:
            source = "const:0"
            needs_constant = True
        po_id = f"po:{output['index']}"
        x, y = coordinates[po_id]
        nodes.append(
            {
                "id": po_id,
                "label": f"PO{output['index']}",
                "type": "output",
                "partition": None,
                "isCutVariable": False,
                "excluded": True,
                "level": max(levels.values(), default=0) + 1,
                "x": round(x, 3),
                "y": round(y, 3),
                "outputIndex": output["index"],
            }
        )
        edge_data.append(
            {
                "id": f"output:{output['index']}",
                "source": source,
                "target": po_id,
                "kind": "output",
                "inverted": lit_inverted(output["lit"]),
                "srcLit": output["lit"],
                "dstLit": None,
                "isCut": False,
                "isOutput": True,
            }
        )
        output_data.append(
            {
                "index": output["index"],
                "literal": output["lit"],
                "driverVariable": driver_var,
                "inverted": lit_inverted(output["lit"]),
                "nodeId": po_id,
            }
        )

    if needs_constant:
        x, y = coordinates["const:0"]
        nodes.append(
            {
                "id": "const:0",
                "label": "CONST",
                "type": "constant",
                "partition": None,
                "isCutVariable": False,
                "excluded": True,
                "level": 0,
                "x": round(x, 3),
                "y": round(y, 3),
            }
        )

    partition_layout = compute_partition_visualization_layout(nodes)
    return {
        "version": 2,
        "mode": "partition",
        "title": Path(aig["path"]).stem,
        "aigFile": aig["path"],
        "keyVariableFile": None,
        "nodes": nodes,
        "edges": edge_data,
        "outputs": output_data,
        "keyVariables": report["cut_variables"],
        "cutVariables": report["cut_variables"],
        "partitionSizes": report["partition_sizes"],
        "partitionLayout": partition_layout,
        "excludedOutputVariables": report["excluded_output_variables"],
        "metrics": {
            "aigVariableCount": aig["maxvar"],
            "andCount": aig["n_ands"],
            "inputCount": aig["n_inputs"],
            "latchCount": aig["n_latches"],
            "outputCount": aig["n_outputs"],
            "metisVertexCount": report["metis_vertex_count"],
            "cutVariableCount": report["cut_variable_count"],
            "cutEdgeCount": report["directed_cut_edge_count"],
        },
    }


def build_key_visualization_data(aig: dict, key_variables: list[int], key_variable_file: Path) -> dict:
    identity_map = {aig_var: aig_var for aig_var in range(1, aig["maxvar"] + 1)}
    no_partitions = [None] * (aig["maxvar"] + 1)
    report = {
        "cut_variables": key_variables,
        "excluded_output_variables": output_driver_variables(aig),
        "cut_edges": [],
        "partition_sizes": {},
        "metis_vertex_count": 0,
        "cut_variable_count": len(key_variables),
        "directed_cut_edge_count": 0,
    }
    data = build_visualization_data(aig, no_partitions, identity_map, report)
    data["mode"] = "key-file"
    data["keyVariableFile"] = str(key_variable_file)
    return data


def write_visualization(path: Path, data: dict) -> None:
    template_path = Path(__file__).with_name("aig_visualization_template.html")
    if not template_path.is_file():
        raise RuntimeError(f"visualization template is missing: {template_path}")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    template = template_path.read_text(encoding="utf-8")
    rendered = template.replace("__AIG_TITLE__", html.escape(data["title"])).replace("__AIG_DATA__", payload)
    path.write_text(rendered, encoding="utf-8")


def find_gpmetis(explicit: str | None, root: Path) -> str | None:
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    candidates.extend(
        [
            root / "METIS" / "build" / "programs" / "gpmetis",
            root / ".local" / "metis" / "bin" / "gpmetis",
        ]
    )
    for candidate in candidates:
        if candidate.is_file() and candidate.stat().st_mode & 0o111:
            return str(candidate)
    return shutil.which("gpmetis")


def run_gpmetis(gpmetis: str, graph_path: Path, parts: int, extra_args: list[str]) -> tuple[list[int], str]:
    cmd = [gpmetis, *extra_args, str(graph_path), str(parts)]
    proc = subprocess.run(cmd, cwd=str(graph_path.parent), text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            "gpmetis failed\n"
            f"command: {' '.join(cmd)}\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}"
        )
    part_path = Path(f"{graph_path}.part.{parts}")
    if not part_path.is_file():
        raise RuntimeError(f"gpmetis did not create partition file: {part_path}")
    return read_partition(part_path, len(read_graph_lines(graph_path)) - 1), proc.stdout


def read_graph_lines(graph_path: Path) -> list[str]:
    return graph_path.read_text(encoding="ascii").splitlines()


def write_partition(part_path: Path, part: list[int]) -> None:
    with part_path.open("w", encoding="ascii") as handle:
        for vertex in range(1, len(part)):
            handle.write(f"{part[vertex]}\n")


def read_partition(part_path: Path, nvertices: int) -> list[int]:
    values = [int(line.strip()) for line in part_path.read_text(encoding="ascii").splitlines() if line.strip()]
    if len(values) != nvertices:
        raise RuntimeError(f"partition file has {len(values)} rows, expected {nvertices}")
    return [-1] + values


def initial_bisection(neighbors: list[set[int]]) -> list[int]:
    nvertices = len(neighbors) - 1
    target = nvertices // 2
    degrees = [len(ns) for ns in neighbors]
    seed = max(range(1, nvertices + 1), key=lambda v: (degrees[v], -v))
    selected = {seed}
    frontier = set(neighbors[seed])

    while len(selected) < target:
        candidates = [v for v in frontier if v not in selected]
        if candidates:
            best = max(
                candidates,
                key=lambda v: (
                    sum(1 for n in neighbors[v] if n in selected),
                    degrees[v],
                    -v,
                ),
            )
        else:
            best = max(
                (v for v in range(1, nvertices + 1) if v not in selected),
                key=lambda v: (degrees[v], -v),
            )
        selected.add(best)
        frontier.update(neighbors[best])

    part = [-1] * (nvertices + 1)
    for vertex in range(1, nvertices + 1):
        part[vertex] = 0 if vertex in selected else 1
    return part


def external_internal_delta(vertex: int, from_part: int, part: list[int], neighbors: list[set[int]]) -> int:
    internal = 0
    external = 0
    for nbr in neighbors[vertex]:
        if part[nbr] == from_part:
            internal += 1
        else:
            external += 1
    return external - internal


def kl_refine(part: list[int], neighbors: list[set[int]], max_passes: int = 25) -> list[int]:
    nvertices = len(part) - 1
    for _ in range(max_passes):
        locked: set[int] = set()
        swaps: list[tuple[int, int, int]] = []
        cumulative = []
        current_gain = 0
        working = part[:]

        for _step in range(nvertices // 2):
            left = [v for v in range(1, nvertices + 1) if working[v] == 0 and v not in locked]
            right = [v for v in range(1, nvertices + 1) if working[v] == 1 and v not in locked]
            if not left or not right:
                break

            d_left = {v: external_internal_delta(v, 0, working, neighbors) for v in left}
            d_right = {v: external_internal_delta(v, 1, working, neighbors) for v in right}
            best_pair = None
            best_gain = None
            for a in left:
                a_neighbors = neighbors[a]
                for b in right:
                    gain = d_left[a] + d_right[b] - (2 if b in a_neighbors else 0)
                    if best_gain is None or gain > best_gain or (gain == best_gain and (a, b) < best_pair):
                        best_gain = gain
                        best_pair = (a, b)

            if best_pair is None or best_gain is None:
                break
            a, b = best_pair
            locked.add(a)
            locked.add(b)
            working[a], working[b] = working[b], working[a]
            swaps.append((a, b, best_gain))
            current_gain += best_gain
            cumulative.append(current_gain)

        if not cumulative:
            break
        best_prefix_gain = max(cumulative)
        if best_prefix_gain <= 0:
            break
        best_prefix_len = cumulative.index(best_prefix_gain) + 1
        for a, b, _gain in swaps[:best_prefix_len]:
            part[a], part[b] = part[b], part[a]

    return part


def count_cut_edges(part: list[int], neighbors: list[set[int]]) -> int:
    return sum(1 for v in range(1, len(neighbors)) for n in neighbors[v] if v < n and part[v] != part[n])


def python_bisect(neighbors: list[set[int]]) -> list[int]:
    part = initial_bisection(neighbors)
    return kl_refine(part, neighbors)


def compute_cuts(
    aig: dict,
    part: list[int],
    neighbors: list[set[int]],
    directed_edges: list[dict],
    aig_to_metis: dict[int, int],
    excluded_output_variables: list[int],
) -> dict:
    unique_directed = {}
    for edge in directed_edges:
        src = edge["src_var"]
        dst = edge["dst_var"]
        src_metis = aig_to_metis.get(src)
        dst_metis = aig_to_metis.get(dst)
        if src_metis is None or dst_metis is None:
            continue
        if part[src_metis] == part[dst_metis]:
            continue
        key = (src, dst, edge["kind"])
        unique_directed[key] = {
            **edge,
            "src_part": part[src_metis],
            "dst_part": part[dst_metis],
        }

    cut_edges = sorted(unique_directed.values(), key=lambda e: (e["src_var"], e["dst_var"], e["kind"]))
    cut_variables = sorted({edge["src_var"] for edge in cut_edges})
    boundary_variables = sorted({edge["src_var"] for edge in cut_edges} | {edge["dst_var"] for edge in cut_edges})
    partition_sizes = Counter(part[1:])

    return {
        "aig_file": aig["path"],
        "aig_header": {
            "maxvar": aig["maxvar"],
            "inputs": aig["n_inputs"],
            "latches": aig["n_latches"],
            "outputs": aig["n_outputs"],
            "ands": aig["n_ands"],
        },
        "metis_vertex_count": len(neighbors) - 1,
        "excluded_output_variable_count": len(excluded_output_variables),
        "excluded_output_variables": excluded_output_variables,
        "partition_sizes": {str(k): partition_sizes[k] for k in sorted(partition_sizes)},
        "undirected_cut_edge_count": count_cut_edges(part, neighbors),
        "directed_cut_edge_count": len(cut_edges),
        "cut_variable_count": len(cut_variables),
        "boundary_variable_count": len(boundary_variables),
        "cut_variables": cut_variables,
        "boundary_variables": boundary_variables,
        "cut_edges": cut_edges,
        "outputs": aig["outputs"],
    }


def write_text_report(path: Path, report: dict) -> None:
    with path.open("w", encoding="utf-8") as handle:
        handle.write(f"AIG: {report['aig_file']}\n")
        handle.write(f"header: {report['aig_header']}\n")
        handle.write(f"metis_vertex_count: {report['metis_vertex_count']}\n")
        handle.write(f"excluded_output_variable_count: {report['excluded_output_variable_count']}\n")
        handle.write(
            "excluded_output_variables: "
            + " ".join(str(v) for v in report["excluded_output_variables"])
            + "\n"
        )
        handle.write(f"vertex_map_file: {report['vertex_map_file']}\n")
        handle.write(f"visualization_file: {report['visualization_file']}\n")
        handle.write(f"partition_sizes: {report['partition_sizes']}\n")
        handle.write(f"undirected_cut_edge_count: {report['undirected_cut_edge_count']}\n")
        handle.write(f"directed_cut_edge_count: {report['directed_cut_edge_count']}\n")
        handle.write(f"cut_variable_count: {report['cut_variable_count']}\n")
        handle.write(f"boundary_variable_count: {report['boundary_variable_count']}\n")
        handle.write("\ncut_variables (source-side AIG vars):\n")
        handle.write(" ".join(str(v) for v in report["cut_variables"]) + "\n")
        handle.write("\nboundary_variables (both endpoints):\n")
        handle.write(" ".join(str(v) for v in report["boundary_variables"]) + "\n")
        handle.write("\ncut_edges (src_var[src_part] -> dst_var[dst_part], kind, inversion):\n")
        for edge in report["cut_edges"]:
            inv = "!" if edge["src_inverted"] else ""
            handle.write(
                f"{inv}{edge['src_var']}[{edge['src_part']}] -> "
                f"{edge['dst_var']}[{edge['dst_part']}] "
                f"{edge['kind']} src_lit={edge['src_lit']} dst_lit={edge['dst_lit']}\n"
            )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("aig", nargs="?", default="test_15_TOP32_72.aig", help="input binary AIGER .aig file")
    parser.add_argument("--parts", type=int, default=2, help="number of partitions; fallback supports only 2")
    parser.add_argument("--out-dir", default="aig_partition_out", help="directory for graph, partition, and reports")
    parser.add_argument("--gpmetis", default=None, help="path to gpmetis; otherwise auto-detected")
    parser.add_argument("--metis-extra", action="append", default=[], help="extra argument passed to gpmetis")
    parser.add_argument("--force-python", action="store_true", help="use the built-in two-way partitioner")
    args = parser.parse_args(argv)

    aig_path = Path(args.aig).expanduser().resolve()
    root = Path.cwd().resolve()
    out_root = Path(args.out_dir).expanduser()
    if not out_root.is_absolute():
        out_root = root / out_root
    out_dir = out_root / aig_path.stem
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.parts < 2:
        raise SystemExit("--parts must be >= 2")

    aig = parse_aig(aig_path)
    neighbors, directed_edges, metis_to_aig, aig_to_metis, excluded_output_variables = build_graph(aig)
    metis_vertex_count = len(neighbors) - 1
    if metis_vertex_count < args.parts:
        raise SystemExit(
            f"cannot partition {metis_vertex_count} METIS vertices into {args.parts} parts "
            f"after excluding output variables {excluded_output_variables}"
        )

    graph_path = out_dir / f"{aig_path.stem}.metis.graph"
    vertex_map_path = out_dir / f"{aig_path.stem}.metis.vertex_map.tsv"
    visualization_path = out_dir / f"{aig_path.stem}.visualization.html"
    edge_count = write_metis_graph(graph_path, neighbors)
    write_vertex_map(vertex_map_path, metis_to_aig)

    method = "python-kl"
    gpmetis_stdout = ""
    part_path = Path(f"{graph_path}.part.{args.parts}")
    if not args.force_python:
        gpmetis = find_gpmetis(args.gpmetis, root)
        if gpmetis:
            part, gpmetis_stdout = run_gpmetis(gpmetis, graph_path, args.parts, args.metis_extra)
            method = f"gpmetis:{gpmetis}"
        elif args.parts != 2:
            raise SystemExit("gpmetis was not found and the built-in fallback only supports --parts 2")
        else:
            part = python_bisect(neighbors)
            write_partition(part_path, part)
    else:
        if args.parts != 2:
            raise SystemExit("--force-python supports only --parts 2")
        part = python_bisect(neighbors)
        write_partition(part_path, part)

    report = compute_cuts(aig, part, neighbors, directed_edges, aig_to_metis, excluded_output_variables)
    report["method"] = method
    report["metis_graph"] = str(graph_path)
    report["metis_edge_count"] = edge_count
    report["vertex_map_file"] = str(vertex_map_path)
    report["visualization_file"] = str(visualization_path)
    report["partition_file"] = str(part_path)
    report["gpmetis_stdout"] = gpmetis_stdout
    visualization_data = build_visualization_data(aig, part, aig_to_metis, report)
    write_visualization(visualization_path, visualization_data)

    json_path = out_dir / f"{aig_path.stem}.cutpoints.json"
    txt_path = out_dir / f"{aig_path.stem}.cutpoints.txt"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_text_report(txt_path, report)

    print(f"method: {method}")
    print(f"aig: {aig_path}")
    print(f"metis_graph: {graph_path}")
    print(f"vertex_map_file: {vertex_map_path}")
    print(f"visualization_file: {visualization_path}")
    print(f"partition_file: {part_path}")
    print(f"json_report: {json_path}")
    print(f"text_report: {txt_path}")
    print(f"partition_sizes: {report['partition_sizes']}")
    print(f"excluded_output_variables: {report['excluded_output_variables']}")
    print(f"undirected_cut_edge_count: {report['undirected_cut_edge_count']}")
    print(f"cut_variable_count: {report['cut_variable_count']}")
    print("cut_variables:")
    print(" ".join(str(v) for v in report["cut_variables"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
