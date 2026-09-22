# channel-metadata-agent

Agentic curation of HTAN imaging **channel metadata** into a harmonized marker library. Each HTAN centre ships its own channel metadata format — different column names, different marker spellings, different conventions for what counts as a target. This turns that pile into one deduplicated, typed, UniProt-resolved list of markers.

> **Status: prototype, second attempt.** Written December 2025 as a rewrite of [`adamjtaylor/ch_metadata_agent`](https://github.com/adamjtaylor/ch_metadata_agent), the pipeline that produced the marker library shipped in [`ncihtan/mif-explorer`](https://github.com/ncihtan/mif-explorer) (Sage Bionetworks Home Week hackathon). This version has better marker typing and full row-level provenance, but it stops before producing app-ready output — nothing here has been wired into the explorer yet.

---

## Input

Channel metadata files attached to HTAN imaging data, fetched by Synapse ID. The upstream fetch lives in the v1 repo (`fetch_htan_data.py`): a BigQuery query over `htan-dcc.released.entities_v7_0` joined to `combined_assays.ImagingLevel2` for every non-MERFISH `channel_metadata_synapseId`, then `syn.get()` of each file. That produces the `htan_data/` directory this pipeline scans — **617 files** (616 CSV, 1 XLSX) across all imaging centres.

The scan is not committed here. Point stage 1.1 at your own copy, or re-fetch with the v1 script.

## Two paths through this repo

Worth knowing before you read the code, because both are present and only one was run to completion:

**The staged scripts (`1_1` → `2_3`)** are the designed pipeline, each a standalone CLI that reads the previous stage's output file. Only stage 1.1 was ever run — `staging_manifest.json` is its output (617 files, scanned 2025-12-15). Stages 1.2 through 2.3 are written and import cleanly but have never been executed end to end; none of their output files exist.

**The notebooks (`dev.ipynb`, `dev_marker_grouping.ipynb`)** are what actually produced the results. They take a different, flatter route: dedupe channel metadata rows by content hash, classify every row in one Batches API pass, then group by extracted marker. This is where `classified_metadata.json` and `classified_markers.json` come from.

So the staged scripts are the cleaner design and the notebooks are the working implementation. Reconciling them is the obvious first task for anyone picking this up.

## The staged pipeline

```
htan_data/ ──1_1──► staging_manifest.json
                      │
                 1_2 (Claude) ──► column_mapping_config.json
                      │
                 1_3 ──► normalized_antibodies.parquet
                      │
                 2_1 ──► unique_term_clusters.json
                      │
                 2_2 (Claude) ──► taxonomy_classifications.json
                      │
                 2_3 (Claude + UniProt) ──► curated_library_definitions.json
```

| Stage | Script | What it does |
|-------|--------|--------------|
| 1.1 | `1_1_scan_stage.py` | Recursively scan for CSV/XLSX, hash each file, emit a staging manifest |
| 1.2 | `1_2_schema_scout.py` | Claude reads each file's header and maps columns to `antibody_name`, `clone_id`, `channel_name`, `panel_id`, `lot_number` |
| 1.3 | `1_3_normalize.py` | Apply the mappings, drop empty and technical rows, consolidate to one parquet |
| 2.1 | `2_1_scrub_and_group.py` | Scrub marker names and cluster variants by string similarity (default threshold 0.85) |
| 2.2 | `2_2_taxonomy_steward.py` | Claude assigns a marker type to each cluster, batched 50 terms per call |
| 2.3 | `2_3_bio_resolver.py` | Claude picks the canonical name following clinical convention, then pulls UniProt metadata |

Every stage takes `-i`/`-o` overrides; run any with `--help`.

## Outputs on disk

| File | Contents |
|------|----------|
| `staging_manifest.json` | 617 scanned channel metadata files with hashes and timestamps |
| `classified_metadata.json` | **3,647** deduplicated channel metadata rows, each with its `row_hash`, original `row_content`, the `ch_synids` it appears in, and a `marker_classification` |
| `classified_markers.json` | The same 3,647 rows regrouped under **621 unique markers**, each typed |

Row-level provenance is the thing this version adds: every marker carries the exact metadata rows and Synapse entities it came from, so any assertion can be traced back to the centre that made it.

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

Separating `blank_or_background` and `chemical_stain` from real protein targets matters — v1 folded these in with everything else, which is why its library contains entries like secondary antibodies and blanks as if they were markers.

## Supporting pieces

- **`uniprot_api.py`** — UniProt REST client with a SQLite cache and a confidence score per match. Returns `UniProtEntry(accession, gene_name, protein_name, organism, subcellular_location, function, confidence_score)`. The cache (`data/cache/uniprot_cache.db`, 5MB, gitignored) is warm from the December runs.
- **`cd_molecules.csv`** — 445 CD molecules with descriptions, used to resolve `cd_marker` entries that UniProt name search handles badly.
- **`lookup_agent.py` / `lookup_claude.py`** — the same UniProt-lookup task written two ways, deliberately: `lookup_claude.py` drives a manual tool-use loop, `lookup_agent.py` uses the Claude Agent SDK. Useful as a side-by-side, not part of the pipeline.

## Running it

```bash
uv venv && uv pip install -r requirements.txt
cp .env.example .env    # add your ANTHROPIC_API_KEY
uv run 1_1_scan_stage.py /path/to/htan_data -o staging_manifest.json
```

Stages 1.2, 2.2, 2.3 and the notebooks call the Anthropic API and cost money to run. Stage 1.1 and `uniprot_api.py` do not.

**Verified** (September 2026): all seven modules parse and expose `--help`; stage 1.1 runs clean against the 617-file `htan_data/` directory; `uniprot_api.lookup_protein("CD8A")` returns 7 ranked hits with `P01732` scored 1.0 first.

## Known limitations

- **Stages 1.2–2.3 are unexecuted.** They parse and their CLIs work, but the chain has never been run end to end. Expect to debug.
- **Model IDs are pinned to December 2025** — `claude-3-5-sonnet-20241022` in stages 2.2 and 2.3, `claude-sonnet-4-5-20250929` in stage 1.2, `claude-haiku-4-5` in the notebooks. The first two are old enough to be worth replacing (current equivalents: `claude-sonnet-5`, `claude-haiku-4-5`) before any rerun. They are left as-is here so the code matches what produced the committed outputs.
- **Structured outputs use the old shape.** The notebooks pass `output_format` with the `structured-outputs-2025-11-13` beta header. Structured outputs are now GA as `output_config: {format: {...}}` with no beta header.
- **Hardcoded absolute defaults.** Several stages default `-i`/`-m` to paths under `/Users/ataylor/Downloads/ch_metadata_agent/`. Always pass paths explicitly.
- **Stage 1.3's default manifest path** points inside `htan_data/` rather than at stage 1.1's actual output location. Pass `-m` explicitly.
- **The notebooks are working notebooks** — outputs committed, cells out of order in places, exploratory dead ends left in.
- **No tests.**

## Next step

Produce app-ready output. `classified_markers.json` has the markers and their provenance but not the joins the explorer needs: marker → panel, panel → file, file → participant/diagnosis/centre. v1's `restructure_antibodies.py` shows the target shape (`library.json`, `manifest.json`, `panels.json`) and is the thing to port onto this better-typed foundation.

## Related

- [`ncihtan/mif-explorer`](https://github.com/ncihtan/mif-explorer) — the explorer prototype these markers feed
- [`adamjtaylor/ch_metadata_agent`](https://github.com/adamjtaylor/ch_metadata_agent) — v1 of this pipeline, whose output shipped in the explorer
