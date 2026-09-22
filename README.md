# channel-metadata-agent

Turns HTAN imaging channel metadata into a harmonized marker library, and from that into the data files the [mIF Explorer](https://github.com/ncihtan/mif-explorer) reads.

Each HTAN centre ships its own channel metadata format: different column names, different marker spellings, different conventions for what counts as a target. This pipeline reads all of them and produces one deduplicated, typed, UniProt-resolved list of markers, with row-level provenance back to the file each assertion came from.

## Input

Channel metadata files attached to HTAN imaging data, fetched by Synapse ID.

Stage 0.1 does the fetch: a BigQuery query for every non-MERFISH `channel_metadata_synapseId` in the release, then `syn.get()` on each. That produces the `htan_data/` directory the rest of the pipeline reads: 714 files as of Release 7.0, a mix of CSV, TSV named `.txt`, and XLSX.

`htan_data/` is not committed. Run stage 0.1, or point stage 1.1 at your own copy.

## Pipeline

```
0_1 (BigQuery + Synapse)      ──► htan_data/
1_1                           ──► staging_manifest.json
1_2 (Claude)                  ──► column_mapping_config.json
1_3                           ──► normalized_antibodies.parquet
2_1                           ──► unique_term_clusters.json
2_2 (Claude)                  ──► taxonomy_classifications.json
2_3 (Claude + UniProt)        ──► curated_library_definitions.json
2_4 (Claude)                  ──► classified_metadata.json + classified_markers.json
3_1 (BigQuery + UniProt)      ──► library / manifest / panels .json
```

| Stage | Script | What it does |
|-------|--------|--------------|
| 0.1 | `0_1_fetch_htan_data.py` | Find every channel metadata file in the release, download each from Synapse |
| 1.1 | `1_1_scan_stage.py` | Scan for channel metadata files, hash each, emit a staging manifest |
| 1.2 | `1_2_schema_scout.py` | Claude maps each file's columns to `antibody_name`, `clone_id`, `channel_name`, `panel_id`, `lot_number` |
| 1.3 | `1_3_normalize.py` | Apply the mappings, drop empty and technical rows, consolidate to one parquet |
| 2.1 | `2_1_scrub_and_group.py` | Scrub marker names, cluster variants by string similarity (default threshold 0.85) |
| 2.2 | `2_2_taxonomy_steward.py` | Claude assigns a marker type to each cluster, batched 50 terms per call |
| 2.3 | `2_3_bio_resolver.py` | Claude picks the canonical name, then pulls UniProt metadata |
| 2.4 | `2_4_extend_curation.py` | Re-extract every metadata row, keep existing classifications, classify only what is new |
| 3.1 | `3_1_build_explorer_data.py` | Join the curated markers to the live HTAN release, emit explorer data files |

Every stage takes `-i`/`-o` overrides. Run any with `--help`.

## Two routes through stages 1.2 to 2.3

The staged scripts and the notebooks (`dev.ipynb`, `dev_marker_grouping.ipynb`) do the same work two ways, and the notebooks are what produced the committed results. They take a flatter route: dedupe metadata rows by content hash, classify every row in one Batches API pass, then group by extracted marker. Stage 1.1 was run; stages 1.2 to 2.3 are written and import cleanly but have not been run end to end.

Stage 3.1 consumes the notebook output, so the chain from raw files to explorer data is complete. Running 1.2 to 2.3 would replace the notebook path with a resumable CLI one, and stage 2.3 would add Claude-chosen canonical names on top of the UniProt resolution 3.1 already does.

Stage 2.4 belongs to the notebook route. It reimplements the notebook's extraction and classification as a CLI and sends only rows whose content hash is not already classified, so it is the stage to run when new files arrive or when a scanning gap is closed. `--dry-run` reports what would be sent without spending anything.

## Outputs

| File | Contents |
|------|----------|
| `staging_manifest.json` | 714 scanned files with hashes and timestamps |
| `classified_metadata.json` | 3,647 deduplicated metadata rows, each with `row_hash`, original `row_content`, the `ch_synids` it appears in, and a marker classification. Stage 2.4 extends this to the 4,499 rows in all 714 files |
| `classified_markers.json` | The same rows regrouped under 621 unique markers, each typed |
| `explorer_data/` | Stage 3.1 output: `library_full.json`, `manifest_full.json`, `panels_full.json`, `*_small.json` fixtures, `coverage_report.json` |

Marker types assigned:

| Type | Count |
|------|------:|
| `protein_single` | 431 |
| `cd_marker` | 81 |
| `blank_or_background` | 33 |
| `other` | 29 |
| `protein_group` | 18 |
| `nuclear_marker` | 13 |
| `chemical_element` | 11 |
| `chemical_stain` | 5 |

Typing separates real protein targets from blanks, stains and bare elements, which v1 folded in together.

[`RESULTS.md`](RESULTS.md) has the summary statistics: how far the centre labels collapse, how panels are shaped, how widely each marker is used, and how much of the vocabulary is shared across centres, with the caveats that matter if you want to compare marker coverage between studies. `uv run summary_stats.py` regenerates every figure in it.

## Stage 3.1: explorer data

```bash
uv run 3_1_build_explorer_data.py
```

Three inputs, three outputs:

- **Manifest** comes from BigQuery on every run, so file counts track the current release. One row per non-MERFISH image file with channel metadata: file ID, Synapse entity, participant, diagnosis, centre, assay type, panel.
- **Panels** come from inverting `classified_markers.json` by `ch_synids`. Each panel gets its harmonized `targets` and a `channels` map from target to the centre's own label.
- **Library** is marker to protein name, UniProt accession, function and subcellular location, resolved from the cache in `uniprot_api.py`. `categories` carries the marker type, so the explorer's existing category filter works unchanged.

Current run against Release 7.0:

```
files 5,982   participants 566   centres 13   assay types 11   diagnoses 24
panels 616 of 714 referenced   markers 621, 474 of 543 protein markers with UniProt
```

Coverage is reported, not assumed. Panels the release references but the curation does not cover are listed in `coverage_report.json` with the file count they affect, so gaps surface instead of shipping as panels with no targets. Today 98 panels are uncovered, affecting 736 files, because stage 1.1 skipped the TSV files (issue #1). Stage 1.1 now reads them; run stage 2.4 to classify the 852 new rows, then rerun 3.1 to close the gap.

## Supporting pieces

- **`uniprot_api.py`** UniProt REST client with a SQLite cache and a confidence score per match. Returns `UniProtEntry(accession, gene_name, protein_name, organism, subcellular_location, function, confidence_score)`. The cache (`data/cache/uniprot_cache.db`, gitignored) covers all 543 protein markers, so stage 3.1 runs offline and free.
- **`summary_stats.py`** reads the curated outputs and prints the collapse, type, panel, reach and cross-centre figures in `RESULTS.md`. No network.
- **`docs/slides.md`** four-slide version of the results, with speaker notes. `pandoc docs/slides.md -t pptx --slide-level=2 -o docs/slides.pptx` rebuilds the deck; `-t revealjs -s --embed-resources -o docs/slides.html` gives a browser version instead.
- **`cd_molecules.csv`** 445 CD molecules with descriptions, for `cd_marker` entries that UniProt name search handles badly.
- **`lookup_agent.py` / `lookup_claude.py`** the same UniProt lookup written two ways, a manual tool-use loop and the Claude Agent SDK. A side-by-side comparison, not part of the pipeline.

## Running it

```bash
uv venv && uv pip install -r requirements.txt
cp .env.example .env    # add your ANTHROPIC_API_KEY
uv run 0_1_fetch_htan_data.py --dry-run          # list what the release references
uv run 0_1_fetch_htan_data.py -o htan_data       # download it
uv run 1_1_scan_stage.py htan_data -o staging_manifest.json
uv run 2_4_extend_curation.py -d htan_data --dry-run     # what is not yet classified
uv run 2_4_extend_curation.py -d htan_data               # classify it
uv run 3_1_build_explorer_data.py --offline
```

Stages 1.2, 2.2, 2.3, 2.4 and the notebooks call the Anthropic API and cost money. Stages 0.1, 1.1 and 3.1 do not. Stages 0.1 and 3.1 need BigQuery access to `htan-dcc`, and 0.1 needs a Synapse login; `--manifest-in` lets 3.1 reuse a previous manifest without BigQuery.

Useful stage 3.1 flags: `--entities-table` to point at a newer release, `--offline` for cached UniProt lookups only, `--min-confidence` for the UniProt acceptance floor (default 0.6, which accepts every non-empty match).

## Notes

- Model IDs are pinned to December 2025: `claude-3-5-sonnet-20241022` in stages 2.2 and 2.3, `claude-sonnet-4-5-20250929` in 1.2, `claude-haiku-4-5` in the notebooks. Left as-is so the code matches what produced the committed outputs. Current equivalents are `claude-sonnet-5` and `claude-haiku-4-5`.
- The notebooks use the old structured-output shape, `output_format` with the `structured-outputs-2025-11-13` beta header. Structured outputs are now GA as `output_config: {format: {...}}` with no header.
- Several stages default `-i`/`-m` to absolute paths under `/Users/ataylor/Downloads/ch_metadata_agent/`. Pass paths explicitly. Stage 1.3's default manifest path points inside `htan_data/` rather than at stage 1.1's output.
- Issue #1: stage 1.1 used to glob `.csv` and `.xlsx` only, so the 97 tab-delimited files named `.txt` were never scanned and the 98 panels referencing them have no curated markers. Stage 1.1 now scans all 714 files and stage 2.4 extends the curation to them. The curated outputs and `explorer_data/` still reflect the 616 CSVs until 2.4 has been run.
- One file, `syn51325432.txt`, has four extra trailing tabs on its last row. Stage 2.4 pads and truncates ragged rows to the header rather than dropping the file.
- No tests.

## Related

- [`ncihtan/mif-explorer`](https://github.com/ncihtan/mif-explorer) the explorer these data files feed
- [`adamjtaylor/ch_metadata_agent`](https://github.com/adamjtaylor/ch_metadata_agent) v1 of this pipeline, whose output shipped in the explorer
