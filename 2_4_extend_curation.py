#!/usr/bin/env python3
"""
Stage 2.4 — Extend the curation to newly scanned channel metadata files.

Re-extracts every channel metadata row from `htan_data/`, keeps the
classifications already in `classified_metadata.json`, classifies whatever is
new with Claude, and rewrites both curated outputs.

This exists because stage 1.1 originally globbed CSV and XLSX only, so the 97
tab-delimited files HTAN centres named `.txt` were never scanned, and neither
were the 98 panels that reference them (issue #1). It is also the stage to run
after an HTAN release adds files.

Row extraction reproduces the notebook exactly: drop the ImagingLevel2 columns
some centres inline into their channel metadata, then hash
`json.dumps(row_content, sort_keys=True)`. Verified against the committed
output: the 616 CSVs reproduce all 3,647 row hashes.

Classification reuses the notebook's prompt, taxonomy and model, so new rows
are typed on the same basis as existing ones. Only new rows are sent, via the
Batches API.

Usage:
    uv run 2_4_extend_curation.py --dry-run          # what would be classified
    uv run 2_4_extend_curation.py                    # classify and rewrite
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

# ImagingLevel2 columns some centres paste into their channel metadata. They
# describe the image, not the channel, and would defeat row deduplication.
DROP_KEYS = [
    "Component", "Filename", "File Format", "HTAN Participant ID",
    "HTAN Parent Biospecimen ID", "HTAN Data File ID",
    "Channel Metadata Filename", "Imaging Assay Type", "Protocol Link",
    "Workflow Start Datetime", "Workflow End Datetime", "Software and Version",
    "Microscope", "Objective", "NominalMagnification", "LensNA",
    "WorkingDistance", "WorkingDistanceUnit", "Pyramid", "Zstack", "Tseries",
    "Passed QC", "Comment", "FOV number", "FOVX", "FOVXUnit", "FOVY",
    "FOVYUnit", "Frame Averaging", "Image ID", "DimensionOrder",
    "PhysicalSizeX", "PhysicalSizeXUnit", "PhysicalSizeY", "PhysicalSizeYUnit",
    "PhysicalSizeZ", "PhysicalSizeZUnit", "Pixels BigEndian", "PlaneCount",
    "SizeC", "SizeT", "SizeX", "SizeY", "SizeZ", "PixelType", "LEVEL", "TYPE",
    "LAYERS", "REGION", "POSITION", "LAYER", "ROUND",
]

MARKER_TYPES = [
    "chemical_stain",
    "blank_or_background",
    "protein_group",
    "protein_single",
    "chemical_element",
    "cd_marker",
    "nuclear_marker",
    "other",
]

MARKER_SCHEMA = {
    "type": "object",
    "properties": {
        "extracted_marker": {
            "type": "string",
            "description": "The most relevant canonical marker or identifier for "
                           "the protein, stain or feature being imaged, extracted "
                           "from the metadata row content.",
        },
        "marker_type": {
            "type": "string",
            "enum": MARKER_TYPES,
            "description": "The type of the canonical marker.",
        },
    },
    "required": ["extracted_marker", "marker_type"],
    "additionalProperties": False,
}

PROMPT = """You are an expert bio-imaging data curator. Your task is to analyze the metadata row content below and extract the canonical target (marker) being imaged.

Metadata Row Content: {row_content}

### Classification Rules
Analyze the extracted value and categorize it into exactly one of the following types:

1. **cd_marker**: STRICTLY for Cluster of Differentiation markers starting with "CD" (e.g., "CD3", "CD45RO", "CD8a").
2. **chemical_stain**: Common histological stains that are not strictly markers.
3. **protein_single**: A specific non-CD protein target (e.g., "Ki67", "FoxP3", "Keratin", "Beta-Catenin").
4. **protein_group**: A broad family of proteins without a specific isoform (e.g., "Caspases", "Histones").
5. **chemical_element**: Elemental isotopes often used in mass cytometry/IMC (e.g., "191Ir", "Ca40", "Na23") if they are the primary focus.
6. **nuclear_marker**: Simple nuclear stains like DAPI, Hoechst, or general mentions of DNA, dsDNA, nucleus etc.
7. **blank_or_background**: Use this if the row indicates a control, empty channel, "Empty", "Blank", or pure background/autofluorescence.
8. **other**: Anything that does not fit the above.

### Extraction Guidelines
- Clean the marker name (remove prefixes like "Anti-" or suffixes like " (FITC)" unless necessary).
- If the content mentions a metal tag AND a protein (e.g., "173Yb_CD3"), extracted_marker should be "CD3" and type should be "cd_marker".

Return the result as a valid JSON object matching the requested schema."""


# ------------------------------------------------------------- row extraction


def json_default(value):
    """Render numpy scalars the way the notebook's pandas dicts rendered them."""
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"not JSON serialisable: {type(value)}")


def read_table(path: Path) -> Optional[pd.DataFrame]:
    """Read one channel metadata file, whatever the centre named it."""
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xls"):
        return pd.read_excel(path)

    sep = "\t" if suffix in (".tsv", ".txt") else ","
    for encoding in ("utf-8", "latin1"):
        try:
            return pd.read_csv(path, sep=sep, encoding=encoding)
        except UnicodeDecodeError:
            continue
        except pd.errors.ParserError:
            return read_ragged(path, sep, encoding)
    return None


def read_ragged(path: Path, sep: str, encoding: str) -> pd.DataFrame:
    """
    Read a file whose rows have more fields than its header.

    One centre's TSV carries trailing empty columns on a single row. Padding
    and truncating to the header keeps the row rather than dropping it.
    """
    with path.open(encoding=encoding, errors="replace") as handle:
        rows = [line.rstrip("\n").split(sep) for line in handle if line.strip()]
    header = rows[0]
    data = [
        [cell if cell != "" else None for cell in row[: len(header)]]
        + [None] * max(0, len(header) - len(row))
        for row in rows[1:]
    ]
    print(f"  {path.name}: ragged, padded to {len(header)} columns")
    return pd.DataFrame(data, columns=header)


def extract_rows(data_dir: Path) -> List[Dict]:
    """
    Deduplicate every channel metadata row in a directory by content.

    Returns one entry per distinct row, listing every channel metadata file
    (`ch_synids`) it appears in.
    """
    suffixes = {".csv", ".tsv", ".txt", ".xlsx", ".xls"}
    seen: Dict[str, Dict] = {}
    order: List[str] = []
    files = 0

    for path in sorted(data_dir.iterdir()):
        if path.suffix.lower() not in suffixes:
            continue
        match = re.search(r"syn\d+", path.name)
        synid = match.group(0) if match else None

        try:
            frame = read_table(path)
        except Exception as error:  # a single unreadable file must not stop the run
            print(f"  {path.name}: unreadable ({error})")
            continue
        if frame is None:
            print(f"  {path.name}: unreadable")
            continue
        files += 1

        for _, row in frame.iterrows():
            content = row.to_dict()
            for key in DROP_KEYS:
                content.pop(key, None)
            row_hash = hashlib.md5(
                json.dumps(content, sort_keys=True, default=json_default).encode("utf-8")
            ).hexdigest()

            if row_hash in seen:
                seen[row_hash]["ch_synids"].append(synid)
            else:
                seen[row_hash] = {
                    "row_hash": row_hash,
                    "row_content": content,
                    "ch_synids": [synid],
                }
                order.append(row_hash)

    print(f"  {files} files, {len(order)} distinct rows")
    return [seen[h] for h in order]


# ------------------------------------------------------------- classification


def classify(rows: List[Dict], model: str, poll_seconds: int) -> Dict[str, Dict]:
    """Classify rows with Claude through the Batches API. Returns hash -> result."""
    from anthropic import Anthropic

    client = Anthropic()
    requests = [
        {
            "custom_id": row["row_hash"],
            "params": {
                "model": model,
                "max_tokens": 100,
                "temperature": 0,
                "messages": [{
                    "role": "user",
                    "content": PROMPT.format(row_content=json.dumps(row["row_content"])),
                }],
                "output_config": {"format": {"type": "json_schema", "schema": MARKER_SCHEMA}},
            },
        }
        for row in rows
    ]

    batch = client.beta.messages.batches.create(requests=requests)
    print(f"  batch {batch.id} submitted, {len(requests)} requests")

    while batch.processing_status != "ended":
        time.sleep(poll_seconds)
        batch = client.beta.messages.batches.retrieve(batch.id)
        print(f"  {batch.processing_status}: {batch.request_counts}")

    results: Dict[str, Dict] = {}
    failed = 0
    for result in client.beta.messages.batches.results(batch.id):
        if result.result.type != "succeeded":
            failed += 1
            continue
        try:
            results[result.custom_id] = json.loads(result.result.message.content[0].text)
        except (json.JSONDecodeError, IndexError):
            failed += 1

    print(f"  {len(results)} classified, {failed} failed")
    return results


# -------------------------------------------------------------------- outputs


def group_markers(metadata: List[Dict]) -> List[Dict]:
    """Regroup classified rows under their marker, as classified_markers.json."""
    grouped: Dict[tuple, List[Dict]] = defaultdict(list)
    for entry in metadata:
        classification = entry.get("marker_classification") or {}
        marker = classification.get("extracted_marker")
        marker_type = classification.get("marker_type")
        if not marker or not marker_type:
            continue
        grouped[(marker, marker_type)].append({
            "row_hash": entry["row_hash"],
            "row_content": entry["row_content"],
            "ch_synids": entry["ch_synids"],
        })

    return [
        {"extracted_marker": marker, "marker_type": marker_type, "entries": entries}
        for (marker, marker_type), entries in sorted(grouped.items())
    ]


def write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, indent=2))
    print(f"  {path}  ({path.stat().st_size / 1024:.0f} KB)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extend the curation to channel metadata rows not yet classified."
    )
    parser.add_argument(
        "-d", "--data-dir", type=Path, default=Path("htan_data"),
        help="Channel metadata directory (default: htan_data/)",
    )
    parser.add_argument(
        "-m", "--metadata", type=Path, default=Path("classified_metadata.json"),
        help="Existing classified rows, rewritten in place (default: classified_metadata.json)",
    )
    parser.add_argument(
        "-k", "--markers", type=Path, default=Path("classified_markers.json"),
        help="Marker-centred output, rewritten in place (default: classified_markers.json)",
    )
    parser.add_argument(
        "--model", default="claude-haiku-4-5",
        help="Model for classification (default: claude-haiku-4-5, as the notebooks used)",
    )
    parser.add_argument(
        "--poll-seconds", type=int, default=15,
        help="Seconds between batch status checks (default: 15)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would be classified, send nothing, write nothing",
    )
    args = parser.parse_args()

    existing = json.loads(args.metadata.read_text())
    known = {
        entry["row_hash"]: entry["marker_classification"]
        for entry in existing
        if entry.get("marker_classification")
    }
    print(f"{len(existing)} rows already curated, {len(known)} with a classification")

    print(f"Extracting rows from {args.data_dir}")
    rows = extract_rows(args.data_dir)

    new = [row for row in rows if row["row_hash"] not in known]
    reused = len(rows) - len(new)
    print(f"  {reused} rows reuse an existing classification, {len(new)} are new")

    dropped = set(known) - {row["row_hash"] for row in rows}
    if dropped:
        print(f"  warning: {len(dropped)} previously curated rows are no longer in {args.data_dir}")

    if args.dry_run:
        if new:
            print("\nExample request:")
            print(PROMPT.format(row_content=json.dumps(new[0]["row_content"]))[:600] + " ...")
        print("\nDry run, nothing sent or written.")
        return

    if not new:
        print("Nothing new to classify.")
    else:
        if not os.getenv("ANTHROPIC_API_KEY"):
            sys.exit("ANTHROPIC_API_KEY is not set. Put it in .env or the environment.")
        print(f"Classifying {len(new)} new rows with {args.model}")
        known.update(classify(new, args.model, args.poll_seconds))

    metadata = [
        {**row, "marker_classification": known.get(row["row_hash"])}
        for row in rows
    ]
    unclassified = sum(1 for entry in metadata if not entry["marker_classification"])
    markers = group_markers(metadata)

    print(f"\n{len(metadata)} rows, {unclassified} unclassified, {len(markers)} markers")
    write_json(args.metadata, metadata)
    write_json(args.markers, markers)


if __name__ == "__main__":
    main()
