#!/usr/bin/env python3
"""
Summary statistics over the curated outputs.

Prints the numbers reported in RESULTS.md: how far the raw channel labels
collapse, what the marker types and UniProt resolution look like, panel shape,
how widely each marker is used, and how much of the vocabulary is shared
across HTAN centres and assay types.

Reads `classified_markers.json` and `explorer_data/`. No network, no API calls.

Usage:
    uv run summary_stats.py
    uv run summary_stats.py --markdown        # tables for pasting into RESULTS.md
"""

import argparse
import json
import statistics as st
from collections import Counter, defaultdict
from pathlib import Path

# The label a centre put on the channel, in the order stage 3.1 prefers.
CHANNEL_NAME_KEYS = [
    "channelname", "targetname", "antibodyname", "markername",
    "markers", "marker", "channel", "antibody",
]
PROTEIN_TYPES = {"protein_single", "protein_group", "cd_marker", "nuclear_marker"}


def raw_label(row_content):
    """The centre's own label for a channel, or None if no name column is present."""
    norm = {
        key.lower().replace(" ", "").replace("_", "").replace("#", ""): value
        for key, value in row_content.items()
    }
    for key in CHANNEL_NAME_KEYS:
        value = norm.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def load(root: Path):
    # classified_markers.json carries literal NaN from pandas, which json rejects.
    markers = json.loads(root.joinpath("classified_markers.json").read_text().replace("NaN", "null"))
    read = lambda name: json.loads(root.joinpath("explorer_data", name).read_text())
    return markers, read("library_full.json"), read("panels_full.json"), read("manifest_full.json")


def main():
    parser = argparse.ArgumentParser(description="Summary statistics over the curated outputs.")
    parser.add_argument("-r", "--root", type=Path, default=Path("."), help="Repo root (default: .)")
    parser.add_argument("--markdown", action="store_true", help="Emit markdown tables")
    args = parser.parse_args()

    markers, library, panels, files = load(args.root)
    bullet = "| " if args.markdown else "  "

    # ---------------------------------------------------------------- collapse
    rows = sum(len(m["entries"]) for m in markers)
    labels = {
        m["extracted_marker"]: {
            label for label in (raw_label(e["row_content"]) for e in m["entries"]) if label
        }
        for m in markers
    }
    distinct_labels = set().union(*labels.values())
    accessions = {e["uniprot"] for e in library.values() if e.get("uniprot")}

    print("## Collapse")
    print(f"{bullet}metadata rows: {rows}")
    print(f"{bullet}distinct centre labels: {len(distinct_labels)}")
    print(f"{bullet}canonical markers: {len(markers)}")
    print(f"{bullet}UniProt accessions: {len(accessions)}")

    spread = Counter(len(v) for v in labels.values())
    print(f"{bullet}markers with one label: {spread[1]}, with three or more: "
          f"{sum(v for k, v in spread.items() if k >= 3)}")
    print("  most-collapsed markers:")
    for marker, labs in sorted(labels.items(), key=lambda kv: -len(kv[1]))[:10]:
        print(f"    {marker:20s} {len(labs):3d}  {', '.join(sorted(labs)[:6])}")

    # ------------------------------------------------------------ types, UniProt
    print("\n## Types")
    for marker_type, count in Counter(m["marker_type"] for m in markers).most_common():
        print(f"{bullet}{marker_type:20s} {count:4d}")
    protein = [m["extracted_marker"] for m in markers if m["marker_type"] in PROTEIN_TYPES]
    resolved = [m for m in protein if library.get(m, {}).get("uniprot")]
    confidence = [library[m]["confidence"] for m in resolved if library[m].get("confidence")]
    print(f"{bullet}protein markers {len(protein)}, resolved {len(resolved)} "
          f"({len(resolved) / len(protein):.0%}), median confidence {st.median(confidence):.2f}")

    shared = defaultdict(list)
    for marker in resolved:
        shared[library[marker]["uniprot"]].append(marker)
    collisions = {a: ms for a, ms in shared.items() if len(ms) > 1}
    print(f"{bullet}accessions carrying more than one marker name: {len(collisions)}, "
          f"covering {sum(len(v) for v in collisions.values())} markers")

    # ----------------------------------------------------------------- panels
    sizes = [len(p["targets"]) for p in panels.values()]
    target_sets = {tuple(sorted(p["targets"])) for p in panels.values()}
    print("\n## Panels")
    print(f"{bullet}panels with targets: {len(panels)}")
    print(f"{bullet}targets per panel: min {min(sizes)}, median {st.median(sizes):.0f}, max {max(sizes)}")
    print(f"{bullet}distinct target sets: {len(target_sets)}")

    # ------------------------------------------------------------------ reach
    files_per_panel = Counter(f["panel_id"] for f in files)
    marker_panels = defaultdict(set)
    for panel_id, panel in panels.items():
        for target in panel["targets"]:
            marker_panels[target].add(panel_id)
    reach = Counter(len(v) for v in marker_panels.values())
    print("\n## Reach")
    for lo, hi, label in [(1, 1, "1 panel"), (2, 4, "2-4"), (5, 19, "5-19"),
                          (20, 99, "20-99"), (100, 10 ** 9, "100+")]:
        print(f"{bullet}{label:8s} {sum(v for k, v in reach.items() if lo <= k <= hi):4d} markers")
    print("  most used: " + ", ".join(
        f"{m} ({len(p)})" for m, p in sorted(marker_panels.items(), key=lambda kv: -len(kv[1]))[:10]
    ))

    # ---------------------------------------------------------- comparability
    panel_centers, panel_assays = defaultdict(set), defaultdict(set)
    for f in files:
        panel_centers[f["panel_id"]].add(f["center"])
        panel_assays[f["panel_id"]].add(f["assay_type"])
    marker_centers, marker_assays = defaultdict(set), defaultdict(set)
    for marker, panel_ids in marker_panels.items():
        for panel_id in panel_ids:
            marker_centers[marker] |= panel_centers[panel_id]
            marker_assays[marker] |= panel_assays[panel_id]

    centers = {c for cs in marker_centers.values() for c in cs}
    by_center = Counter(len(v) for v in marker_centers.values())
    core = sorted(
        (m for m, cs in marker_centers.items() if len(cs) >= len(centers) // 2 + 1),
        key=lambda m: -len(marker_centers[m]),
    )
    print("\n## Comparability")
    print(f"{bullet}markers at one centre only: {by_center[1]} "
          f"({by_center[1] / len(marker_centers):.0%})")
    print(f"{bullet}markers at three or more centres: "
          f"{sum(v for k, v in by_center.items() if k >= 3)}")
    print(f"{bullet}markers in one assay type only: "
          f"{Counter(len(v) for v in marker_assays.values())[1]}")
    print(f"{bullet}markers at more than half of {len(centers)} centres: {len(core)}")
    print("  " + ", ".join(core))

    print("\n  centre vocabularies:")
    center_markers = defaultdict(set)
    for panel_id, panel in panels.items():
        for center in panel_centers[panel_id]:
            center_markers[center] |= set(panel["targets"])
    for center, ms in sorted(center_markers.items(), key=lambda kv: -len(kv[1])):
        unique = sum(1 for m in ms if marker_centers[m] == {center})
        print(f"    {center:22s} {len(ms):4d} markers, {unique:4d} seen nowhere else")


if __name__ == "__main__":
    main()
