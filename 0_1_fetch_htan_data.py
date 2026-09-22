#!/usr/bin/env python3
"""
Stage 0.1 — Fetch HTAN channel metadata files.

Finds every channel metadata file attached to non-MERFISH HTAN imaging data in
BigQuery, then downloads each one from Synapse into a local directory. That
directory is the input to stage 1.1.

Files are saved as `{synapseId}.{ext}`, keeping whatever extension the centre
uploaded. Expect a mix: CSV, TSV named `.txt`, and XLSX.

Usage:
    uv run 0_1_fetch_htan_data.py --dry-run            # what would be fetched
    uv run 0_1_fetch_htan_data.py -o htan_data
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Dict, List

RECORDS_SQL = """
SELECT DISTINCT
  r.channel_metadata_synapseId AS panel_id,
  r.channel_metadata_version   AS panel_version,
  i.Imaging_Assay_Type         AS assay_type,
  i.HTAN_Center                AS center
FROM `{entities_table}` r
JOIN `htan-dcc.combined_assays.ImagingLevel2` i
  USING (entityId)
WHERE r.channel_metadata_synapseId IS NOT NULL
  AND i.Imaging_Assay_Type != 'MERFISH'
ORDER BY panel_id
"""


def query_records(entities_table: str) -> List[Dict]:
    """List the channel metadata files the release references."""
    try:
        from google.cloud import bigquery
    except ImportError:
        sys.exit(
            "google-cloud-bigquery is not installed.\n"
            "  uv pip install google-cloud-bigquery"
        )

    sql = RECORDS_SQL.format(entities_table=entities_table)
    print(f"Querying BigQuery ({entities_table}) ...")
    rows = bigquery.Client().query(sql).result()
    records = [dict(row) for row in rows]
    print(f"  {len(records)} channel metadata files, "
          f"{len({r['center'] for r in records})} centres, "
          f"{len({r['assay_type'] for r in records})} assay types")
    return records


def download(records: List[Dict], outdir: Path) -> None:
    """Download each file from Synapse, naming it by Synapse ID."""
    try:
        import synapseclient
    except ImportError:
        sys.exit("synapseclient is not installed.\n  uv pip install synapseclient")

    syn = synapseclient.login()
    outdir.mkdir(parents=True, exist_ok=True)

    for index, record in enumerate(records, start=1):
        target = f"{record['panel_id']}.{record['panel_version']}"
        entity = syn.get(target, downloadLocation=str(outdir), silent=True)
        ext = os.path.splitext(entity.path)[1]
        final = outdir / f"{entity.id}{ext}"
        if Path(entity.path) != final:
            os.replace(entity.path, final)
        print(f"  [{index}/{len(records)}] {final.name}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch HTAN channel metadata files from Synapse."
    )
    parser.add_argument(
        "-o", "--outdir", type=Path, default=Path("htan_data"),
        help="Download directory (default: htan_data/)",
    )
    parser.add_argument(
        "--entities-table", default="htan-dcc.released.entities_v7_0",
        help="Release table to query (default: htan-dcc.released.entities_v7_0)",
    )
    parser.add_argument(
        "--records-out", type=Path, default=Path("htan_channel_metadata_records.json"),
        help="Where to write the query result (default: htan_channel_metadata_records.json)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Query and write the record list, but download nothing",
    )
    args = parser.parse_args()

    records = query_records(args.entities_table)
    args.records_out.write_text(json.dumps(records, indent=2) + "\n")
    print(f"Wrote {args.records_out}")

    if args.dry_run:
        print("Dry run, nothing downloaded.")
        return

    download(records, args.outdir)


if __name__ == "__main__":
    main()
