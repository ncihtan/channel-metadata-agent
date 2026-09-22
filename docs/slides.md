---
title: "Harmonizing HTAN imaging channel metadata"
subtitle: "channel-metadata-agent, Release 7.0"
author: "Adam Taylor"
---

## Thirteen formats, one vocabulary

HTAN multiplexed imaging, Release 7.0: **5,982 files**, 566 participants, **13 centres**, 11 assay types, 24 diagnoses.

Every centre ships its own channel metadata. Different column names, different marker spellings, different views on what counts as a target. Individually legible, collectively unqueryable.

`channel-metadata-agent` reads all 714 channel metadata files and emits one typed, UniProt-resolved marker list, with row-level provenance back to the file each assertion came from.

```
Synapse channel metadata ─┐
                          ├─► curated markers ─► panels ─┐
BigQuery release tables ──┘                              ├─► manifest / panels / library
UniProt ─────────────────────────────────────────────────┘
```

::: notes
The pipeline is an LLM classification pass over deduplicated metadata rows, followed by UniProt resolution with a SQLite cache. Stage 3.1 re-queries BigQuery on every run, so corpus counts track the release rather than a snapshot.
:::

## What collapses

```
3,647 metadata rows
  └─► 1,013 distinct centre labels
        └─► 621 canonical markers
              └─► 413 UniProt accessions
```

459 markers were written one way only. 82 were written three or more ways. 543 markers are protein-like; 474 of those resolve to UniProt.

616 panels carry targets, median 28 each, but they describe only **125 distinct target sets**: most panels are a standard panel reapplied.

| Marker | Labels | How centres wrote it |
|---|---:|---|
| Ki67 | 13 | `KI67`, `Ki-67`, `Antigen Ki67`, `04_Ki-67_Argo555L` |
| PD-L1 | 12 | `PD-L1 (28-8)`, `PD-L1 (E1L3N)`, `11_PD-L1_Argo662` |
| Vimentin | 10 | `VIM`, `VIMENTIN`, `Vimentin (D)`, `Target:Vimentin` |
| E-cadherin | 10 | `E-Cad`, `ECAD`, `Ecad`, `13_E-Cadherin_Argo730` |
| Granzyme B | 8 | `GRZB`, `GZMB`, `GrzB`, `GramzymB` |

::: notes
DNA and DAPI top the collapse table with 69 and 48 labels, but that is cyclic imaging numbering the nuclear channel per cycle rather than genuine disagreement, so they are left out here.
:::

## What it enables: comparing studies

**28 markers appear at more than half of the 13 centres.** That is the practical intersection for any cross-study comparison:

> CD4, CD68, FOXP3, Ki67, CD20, CD45, CD8, CD31, PD-L1, Vimentin, CD163, CD3, PD1, HLA-DR, CD11B, DAPI, E-cadherin, CD11c, CD14, CD44, DNA, PanCytokeratin, Granzyme B, PanCK, CD45RO, Podoplanin, Lag3, CD3e

Against that, **405 of 621 markers (65%) appear at a single centre**, and 480 in a single assay type.

An immune and structural backbone shared across centres, plus a large private extension each. Intersection comparisons are limited to roughly 28 markers; union comparisons have to reason about absence, because a missing marker means the centre did not stain for it.

```
manifest ──panel_id──► panels ──target──► library
centre, assay,         targets,           uniprot, function,
participant, diagnosis channels           location, type
```

| Centre | Markers | Seen nowhere else |
|---|---:|---:|
| HMS | 196 | 80 |
| TNP SARDANA | 182 | 67 |
| OHSU | 143 | 26 |
| Stanford | 115 | 64 |
| ... | | |
| BU | 22 | 3 |

::: notes
This is the substrate a comparison program would build on. Recommended matching ladder for an external marker: exact canonical name, then UniProt accession, then manual review with the decision recorded.
:::

## Caveats and what is next

Known gaps, so nobody builds on them unaware:

- **Names are not fully synonym-collapsed.** `CD3`, `CD3d`, `CD3e` are separate entries, as are `PanCK`, `PanCytokeratin`, `Cytokeratin (Pan)`. The classifier saw one row at a time and could not merge across rows.
- **53 UniProt accessions carry more than one marker name.** Most are true synonyms; some are resolution errors. P02533 is attached to `Keratin 5`, `Keratin 14`, `Keratin 17` and bare `Keratin`, several of which co-occur in the same 17 panels.
- **Confidence is heuristic**, not curated: 231 of 474 at 0.9 or above, 243 between 0.6 and 0.9.
- **78 of 621 markers are not targets**: blanks, empty channels, bare isotopes. Filter on marker type.
- **736 files have no targets yet**, from 98 panels the curation has not reached.

Next: classify the 852 rows from the 97 tab-delimited files stage 1.1 used to skip, taking panel coverage to 714 of 714, then re-derive every number above.

Full numbers in `RESULTS.md`, all reproducible with `uv run summary_stats.py`. Consumed today by [mif-explorer](https://github.com/ncihtan/mif-explorer).

::: notes
The scanning gap and the extension stage are issue #1 and stage 2.4 respectively. The classification pass is a single Haiku batch job, about fifteen cents.
:::
