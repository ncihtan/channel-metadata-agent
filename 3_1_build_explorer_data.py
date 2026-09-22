#!/usr/bin/env python3
"""
Stage 3.1 — Build explorer-ready data.

Takes the curated marker output of this pipeline (`classified_markers.json`) and
the live HTAN release in BigQuery, and emits the three JSON files the mIF
Explorer reads:

    library_full.json    marker -> protein biology + marker type
    manifest_full.json   one row per image file, with panel/participant/centre
    panels_full.json     panel (channel metadata synId) -> targets + channel names

Plus `*_small.json` variants for development.

The manifest is re-derived from BigQuery every run, so file counts track the
current HTAN release. Only the marker curation is frozen: it comes from
`classified_markers.json`. Panels the release references but the curation does
not cover are listed in the coverage report rather than silently dropped.

Protein biology comes from UniProt via `uniprot_api.py`, which is cache-first.
No LLM calls, so this stage is free to run.

Usage:
    uv run 3_1_build_explorer_data.py                 # BigQuery + UniProt (network)
    uv run 3_1_build_explorer_data.py --offline       # cached UniProt lookups only
    uv run 3_1_build_explorer_data.py --manifest-in manifest_full.json  # skip BigQuery
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

from uniprot_api import UniProtAPI, UniProtEntry

# Marker types that name a protein and are therefore worth resolving against
# UniProt. Blanks, stains and bare chemical elements are kept in the library
# (the explorer counts them as panel channels) but carry no protein biology.
PROTEIN_TYPES = {
    "protein_single",
    "protein_group",
    "cd_marker",
    "nuclear_marker",
}

# Raw column names that hold the marker as the centre wrote it, best first.
# Used for panels[panel_id].channels[target] — the channel label shown next to
# the harmonized target name in the UI.
CHANNEL_NAME_KEYS = [
    "channelname",
    "targetname",
    "antibodyname",
    "markername",
    "markers",
    "marker",
    "channel",
    "antibody",
]

MANIFEST_SQL = """
WITH dx AS (
  SELECT
    HTAN_Participant_ID,
    ARRAY_AGG(Primary_Diagnosis ORDER BY Primary_Diagnosis LIMIT 1)[OFFSET(0)] AS diagnosis
  FROM `htan-dcc.combined_assays.Diagnosis`
  WHERE Primary_Diagnosis IS NOT NULL
  GROUP BY HTAN_Participant_ID
)
SELECT DISTINCT
  i.HTAN_Data_File_ID  AS file_id,
  i.entityId           AS entity_id,
  i.HTAN_Participant_ID AS participant_id,
  dx.diagnosis         AS diagnosis,
  i.HTAN_Center        AS center,
  i.Imaging_Assay_Type AS assay_type,
  r.channel_metadata_synapseId AS panel_id
FROM `htan-dcc.combined_assays.ImagingLevel2` i
JOIN `{entities_table}` r
  USING (entityId)
LEFT JOIN dx
  ON dx.HTAN_Participant_ID = i.HTAN_Participant_ID
WHERE r.channel_metadata_synapseId IS NOT NULL
  AND i.Imaging_Assay_Type != 'MERFISH'
ORDER BY file_id
"""


def normalise_key(key: str) -> str:
    """Lowercase a column name and strip everything that is not alphanumeric."""
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


# ---------------------------------------------------------------- manifest


def fetch_manifest(entities_table: str) -> List[Dict]:
    """Query BigQuery for every non-MERFISH image file that has channel metadata."""
    try:
        from google.cloud import bigquery
    except ImportError:
        sys.exit(
            "google-cloud-bigquery is not installed.\n"
            "  uv pip install google-cloud-bigquery\n"
            "Or skip the query with --manifest-in <manifest_full.json>."
        )

    sql = MANIFEST_SQL.format(entities_table=entities_table)
    print(f"Querying BigQuery ({entities_table}) ...")
    rows = bigquery.Client().query(sql).result()

    manifest = [
        {
            "file_id": row["file_id"],
            "entity_id": row["entity_id"],
            "participant_id": row["participant_id"],
            "diagnosis": row["diagnosis"],
            "center": row["center"],
            "assay_type": row["assay_type"],
            "panel_id": row["panel_id"],
        }
        for row in rows
    ]
    print(f"  {len(manifest)} files")
    return manifest


# ------------------------------------------------------------------ panels


def build_panels(markers: List[Dict]) -> Dict[str, Dict]:
    """
    Invert classified_markers.json into panel -> {targets, channels}.

    Each marker carries the metadata rows it was extracted from, and each row
    carries the channel metadata files (`ch_synids`) it appears in. Those
    Synapse IDs are the panel IDs the manifest joins on.
    """
    targets: Dict[str, set] = defaultdict(set)
    channels: Dict[str, Dict[str, str]] = defaultdict(dict)

    for marker in markers:
        name = marker["extracted_marker"]
        for entry in marker.get("entries", []):
            raw = raw_channel_name(entry.get("row_content", {}))
            for panel_id in entry.get("ch_synids", []):
                targets[panel_id].add(name)
                # First row wins — panels repeat a marker across cycles.
                if raw and name not in channels[panel_id]:
                    channels[panel_id][name] = raw

    return {
        panel_id: {
            "targets": sorted(names),
            "channels": {k: channels[panel_id][k] for k in sorted(channels[panel_id])},
        }
        for panel_id, names in sorted(targets.items())
    }


def raw_channel_name(row_content: Dict) -> Optional[str]:
    """Pull the centre's own label for this channel out of a metadata row."""
    by_norm = {normalise_key(k): v for k, v in row_content.items()}
    for key in CHANNEL_NAME_KEYS:
        value = by_norm.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text and text.lower() not in {"nan", "none", ""}:
            return text
    return None


def annotate_panels(panels: Dict[str, Dict], manifest: List[Dict]) -> None:
    """Add centre and assay type to each panel from the files that used it."""
    centers: Dict[str, set] = defaultdict(set)
    assays: Dict[str, set] = defaultdict(set)
    for row in manifest:
        centers[row["panel_id"]].add(row["center"])
        assays[row["panel_id"]].add(row["assay_type"])

    for panel_id, panel in panels.items():
        panel["center"] = ", ".join(sorted(centers.get(panel_id, [])))
        panel["assay_type"] = ", ".join(sorted(assays.get(panel_id, [])))


# ----------------------------------------------------------------- library


def build_library(
    markers: List[Dict],
    cache_dir: Path,
    offline: bool,
    min_confidence: float,
) -> Dict[str, Dict]:
    """
    Build marker -> library entry, in the shape `types.ts` declares.

    `categories` carries the marker type from stage 2.2 so the explorer's
    existing category filter separates real protein targets from blanks,
    stains and bare elements without any UI change.
    """
    api = UniProtAPI(cache_dir)
    library: Dict[str, Dict] = {}
    resolved = 0

    protein_markers = [m for m in markers if m["marker_type"] in PROTEIN_TYPES]
    print(
        f"Resolving {len(protein_markers)} protein markers against UniProt "
        f"({'cache only' if offline else 'cache first, then API'}) ..."
    )

    for marker in sorted(markers, key=lambda m: m["extracted_marker"]):
        name = marker["extracted_marker"]
        marker_type = marker["marker_type"]

        entry: Dict = {
            "protein_name": None,
            "uniprot": None,
            "function": None,
            "location": [],
            "categories": [marker_type],
            "marker_type": marker_type,
            "n_panels": len({s for e in marker.get("entries", []) for s in e.get("ch_synids", [])}),
            "n_rows": len(marker.get("entries", [])),
        }

        if marker_type in PROTEIN_TYPES:
            hit = best_uniprot_match(api, name, offline, min_confidence)
            if hit:
                entry.update(
                    protein_name=hit.protein_name,
                    uniprot=hit.accession,
                    function=hit.function,
                    location=hit.subcellular_location,
                    organism=hit.organism,
                    confidence=round(hit.confidence_score, 3),
                )
                if hit.gene_name:
                    entry["gene_name"] = hit.gene_name
                resolved += 1

        library[name] = entry

    print(f"  {resolved}/{len(protein_markers)} protein markers resolved")
    return library


def best_uniprot_match(
    api: UniProtAPI,
    query: str,
    offline: bool,
    min_confidence: float,
) -> Optional[UniProtEntry]:
    """Highest-confidence human UniProt hit for a marker name, or None."""
    if offline:
        cached = api.cache.get(f"search:{query}:human:True")
        if cached is None:
            return None
        hits = [UniProtEntry(**h) for h in cached]
    else:
        hits = api.search_protein(query)

    hits = [h for h in hits if h.confidence_score >= min_confidence]
    if not hits:
        return None
    return max(hits, key=lambda h: h.confidence_score)


# ---------------------------------------------------------------- coverage


def coverage_report(
    manifest: List[Dict],
    panels: Dict[str, Dict],
    library: Dict[str, Dict],
) -> Dict:
    """
    Compare what BigQuery references against what the curation covers.

    Today's release is fully covered; this is here so the next HTAN release
    announces its gap instead of quietly shipping panels with no targets.
    """
    referenced = {row["panel_id"] for row in manifest}
    defined = set(panels)
    missing = sorted(referenced - defined)
    orphan = sorted(defined - referenced)

    files_without_targets = sum(1 for row in manifest if row["panel_id"] in missing)
    resolved = sum(1 for entry in library.values() if entry["uniprot"])

    report = {
        "files": len(manifest),
        "participants": len({r["participant_id"] for r in manifest}),
        "centers": len({r["center"] for r in manifest}),
        "assay_types": len({r["assay_type"] for r in manifest}),
        "diagnoses": len({r["diagnosis"] for r in manifest if r["diagnosis"]}),
        "panels_referenced": len(referenced),
        "panels_defined": len(defined),
        "panels_missing_definition": missing,
        "panels_never_used": orphan,
        "files_with_no_targets": files_without_targets,
        "markers": len(library),
        "markers_uniprot_resolved": resolved,
    }

    pct = 100 * (len(referenced) - len(missing)) / max(len(referenced), 1)
    print("\nCoverage")
    print(f"  files                  {report['files']}")
    print(f"  participants           {report['participants']}")
    print(f"  centres                {report['centers']}")
    print(f"  assay types            {report['assay_types']}")
    print(f"  diagnoses              {report['diagnoses']}")
    print(f"  panels referenced      {report['panels_referenced']}")
    print(f"  panels with targets    {len(referenced) - len(missing)}  ({pct:.1f}%)")
    print(f"  panels uncurated       {len(missing)}  ({files_without_targets} files show no targets)")
    print(f"  panels never used      {len(orphan)}")
    print(f"  markers                {report['markers']}")
    print(f"  markers with UniProt   {resolved}")
    if missing:
        print("\n  Uncurated panels — re-run the pipeline over these channel metadata files:")
        for panel_id in missing[:20]:
            print(f"    {panel_id}")
        if len(missing) > 20:
            print(f"    ... and {len(missing) - 20} more")
    return report


# ----------------------------------------------------------------- outputs


def write_small(
    outdir: Path,
    manifest: List[Dict],
    panels: Dict[str, Dict],
    library: Dict[str, Dict],
    n_files: int,
) -> None:
    """Write a coherent development subset: n files, their panels, their markers."""
    subset = manifest[:n_files]
    panel_ids = {row["panel_id"] for row in subset}
    small_panels = {p: panels[p] for p in sorted(panel_ids) if p in panels}
    targets = {t for panel in small_panels.values() for t in panel["targets"]}
    small_library = {t: library[t] for t in sorted(targets) if t in library}

    write_json(outdir / "manifest_small.json", subset)
    write_json(outdir / "panels_small.json", small_panels)
    write_json(outdir / "library_small.json", small_library)


def write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n")
    size = path.stat().st_size / 1024
    print(f"  {path}  ({size:.0f} KB)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build mIF Explorer data from curated markers + the live HTAN release."
    )
    parser.add_argument(
        "-c", "--classified", type=Path, default=Path("classified_markers.json"),
        help="Curated marker output of the notebooks (default: classified_markers.json)",
    )
    parser.add_argument(
        "-o", "--outdir", type=Path, default=Path("explorer_data"),
        help="Where to write the JSON (default: explorer_data/)",
    )
    parser.add_argument(
        "--entities-table", default="htan-dcc.released.entities_v7_0",
        help="Release table to join (default: htan-dcc.released.entities_v7_0). "
             "Point at the newest entities_vX_Y after an HTAN release.",
    )
    parser.add_argument(
        "--manifest-in", type=Path,
        help="Reuse a previously written manifest JSON instead of querying BigQuery",
    )
    parser.add_argument(
        "--cache-dir", type=Path, default=Path("data/cache"),
        help="UniProt SQLite cache directory (default: data/cache)",
    )
    parser.add_argument(
        "--offline", action="store_true",
        help="Use only cached UniProt lookups — no network calls",
    )
    parser.add_argument(
        "--min-confidence", type=float, default=0.6,
        help="Minimum UniProt match confidence to accept (default: 0.6, which "
             "accepts every non-empty match; 0.9 requires an exact gene-name hit)",
    )
    parser.add_argument(
        "--small", type=int, default=250,
        help="Number of files in the *_small.json fixtures (default: 250)",
    )
    args = parser.parse_args()

    markers = json.loads(args.classified.read_text())
    print(f"Loaded {len(markers)} curated markers from {args.classified}")

    if args.manifest_in:
        manifest = json.loads(args.manifest_in.read_text())
        print(f"Loaded {len(manifest)} manifest rows from {args.manifest_in}")
    else:
        manifest = fetch_manifest(args.entities_table)

    panels = build_panels(markers)
    annotate_panels(panels, manifest)
    library = build_library(markers, args.cache_dir, args.offline, args.min_confidence)

    report = coverage_report(manifest, panels, library)

    args.outdir.mkdir(parents=True, exist_ok=True)
    print("\nWriting")
    write_json(args.outdir / "manifest_full.json", manifest)
    write_json(args.outdir / "panels_full.json", panels)
    write_json(args.outdir / "library_full.json", library)
    write_small(args.outdir, manifest, panels, library, args.small)
    write_json(args.outdir / "coverage_report.json", report)


if __name__ == "__main__":
    main()
