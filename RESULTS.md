# Results

What the pipeline produced from HTAN Release 7.0, and what the numbers mean for a program that wants to compare marker coverage across imaging studies.

Every figure here is printed by `uv run summary_stats.py`, which reads `classified_markers.json` and `explorer_data/`. Nothing is hand-counted. Counts reflect the 616 CSV channel metadata files curated so far; 98 panels are not yet covered ([issue #1](https://github.com/ncihtan/channel-metadata-agent/issues/1)).

## Corpus

| | |
|---|---:|
| Image files | 5,982 |
| Participants | 566 |
| Centres | 13 |
| Assay types | 11 |
| Diagnoses | 24 |
| Panels referenced by the release | 714 |
| Panels with curated targets | 616 |

## What collapses

The point of the pipeline is to turn each centre's own channel labels into one vocabulary. Four levels, each narrower than the last:

| Level | Count | What it is |
|---|---:|---|
| Metadata rows | 3,647 | Distinct channel definitions after deduplicating by content hash |
| Centre labels | 1,013 | Distinct strings centres wrote in their channel or target name column |
| Canonical markers | 621 | What the classifier extracted, typed |
| UniProt accessions | 413 | Proteins, for the 474 markers that resolved |

So roughly 1,013 labels reduce to 621 markers, and the 474 of those that are resolvable proteins reduce to 413 accessions.

The collapse is uneven. 459 markers were written one way only; 82 were written three or more ways.

| Marker | Labels | Examples |
|---|---:|---|
| DNA | 69 | `DNA`, `DNA (1)`, `DNA (10)`, `DNA (12)` |
| DAPI | 48 | `DAPI`, `DAPI-01`, `DAPI-02`, `DAPI-07` |
| Ki67 | 13 | `KI67`, `Ki-67`, `Antigen Ki67`, `04_Ki-67_Argo555L` |
| PD-L1 | 12 | `PD-L1`, `PD-L1 (28-8)`, `PD-L1 (E1L3N)`, `11_PD-L1_Argo662` |
| Vimentin | 10 | `VIM`, `VIMENTIN`, `Vimentin (D)`, `Target:Vimentin` |
| E-cadherin | 10 | `E-Cad`, `ECAD`, `Ecad`, `13_E-Cadherin_Argo730` |
| PanCK | 8 | `PANCK`, `Pan-CK`, `panCK`, `Cytokeratin (pan)` |
| Granzyme B | 8 | `GRZB`, `GZMB`, `GrzB`, `GramzymB` |

The two nuclear markers at the top are inflated by cyclic imaging: centres number the nuclear channel per cycle, so `DNA (1)` through `DNA (15)` are the same stain imaged repeatedly. The rest are genuine spelling, casing and clone-suffix variation, plus fluorophore-tagged names from Orion-style panels.

## Marker types

| Type | Count |
|---|---:|
| `protein_single` | 431 |
| `cd_marker` | 81 |
| `blank_or_background` | 33 |
| `other` | 29 |
| `protein_group` | 18 |
| `nuclear_marker` | 13 |
| `chemical_element` | 11 |
| `chemical_stain` | 5 |

543 markers are protein-like (`protein_single`, `protein_group`, `cd_marker`, `nuclear_marker`). 474 of those resolved against UniProt, 87%, at a median confidence of 0.80. 472 are human; the two non-human hits are viral antigens.

The remaining 78 markers are blanks, empty channels, isotope labels and unclassifiable strings. They are real rows in the metadata but they are not targets, so any comparison should filter on `marker_type`.

## Panels

616 panels carry targets, with a median of 28 targets each and a range of 1 to 57. Those 616 panels describe only **125 distinct target sets**, so most panels are a standard panel reapplied to another slide rather than a new experiment.

## How widely each marker is used

| Panels containing the marker | Markers |
|---|---:|
| 1 | 238 |
| 2 to 4 | 150 |
| 5 to 19 | 111 |
| 20 to 99 | 77 |
| 100 or more | 45 |

The most used markers are CD68 (552 panels), CD4 (528), CD20 (513), FOXP3 (511), CD45 (482), Vimentin (473), CD8 (459), CD11B (454), SMA (452) and DAPI (433). The distribution is strongly long-tailed: 38% of markers appear in exactly one panel.

## Cross-study comparability

This is the part that matters for comparing studies.

- **405 of 621 markers (65%) appear at a single centre.** 480 appear in a single assay type.
- **113 markers appear at three or more centres.**
- **28 markers appear at more than half of the 13 centres.** These are the practical common denominator:

  CD4, CD68, FOXP3, Ki67, CD20, CD45, CD8, CD31, PD-L1, Vimentin, CD163, CD3, PD1, HLA-DR, CD11B, DAPI, E-cadherin, CD11c, CD14, CD44, DNA, PanCytokeratin, Granzyme B, PanCK, CD45RO, Podoplanin, Lag3, CD3e

| Centre | Markers | Seen nowhere else |
|---|---:|---:|
| HTAN HMS | 196 | 80 |
| HTAN TNP SARDANA | 182 | 67 |
| HTAN OHSU | 143 | 26 |
| HTAN Stanford | 115 | 64 |
| HTAN TNP - TMA | 95 | 4 |
| HTAN WUSTL | 91 | 31 |
| HTAN HTAPP | 90 | 34 |
| HTAN CHOP | 87 | 44 |
| HTAN Vanderbilt | 60 | 18 |
| HTAN DFCI | 49 | 5 |
| HTAN MSK | 47 | 17 |
| HTAN Duke | 45 | 12 |
| HTAN BU | 22 | 3 |

The shape is an immune and structural backbone shared across centres, with each centre adding a large private extension. A cross-study comparison built on the intersection will be restricted to around 28 markers. One built on the union has to reason explicitly about absence, because a marker missing from a panel usually means the centre did not stain for it, not that it was absent from the tissue.

## Using these outputs as a comparison substrate

Three files, joined on two keys:

```
manifest_full.json   file  ──panel_id──►  panels_full.json  ──target──►  library_full.json
                     centre, assay type,   targets, channels           uniprot, function,
                     participant, diagnosis                            location, categories
```

A reasonable matching ladder for aligning a marker from an external study:

1. Exact match on the canonical marker name in `library_full.json`.
2. Match on UniProt accession, which absorbs synonym pairs the classifier kept apart.
3. Fall back to manual review, and record the decision, because the two steps above have known gaps.

Those gaps, stated plainly:

- **The canonical name is not fully synonym-collapsed.** `CD3`, `CD3d` and `CD3e` are separate entries, as are `PanCK`, `PanCytokeratin` and `Cytokeratin (Pan)`, and `DNA`, `DAPI` and `Hoechst 33342`. The classifier worked one row at a time and had no view of the whole vocabulary, so it could not merge across rows. Union these deliberately rather than assuming one name per protein.
- **Accession collisions are mostly synonyms, but not all.** 53 accessions carry more than one marker name, covering 114 markers. Most are harmless (`AR` and `Androgen Receptor`, `AIF-1` and `IBA1`). Some are resolution errors: P02533 is attached to `Keratin`, `Keratin 5`, `Keratin 14`, `Keratin 17` and `Cytokeratin 14`, and several of those co-occur in 17 of the same panels, so they cannot be the same target. Treat accession as a strong hint, not proof.
- **UniProt confidence is heuristic.** Of the 474 resolved markers, 231 scored 0.9 or above and 243 fell between 0.6 and 0.9. The score comes from string agreement between the marker and the UniProt gene or protein name, not from curation.
- **The centre's own label survives.** `panels_full.json` keeps a `channels` map from canonical target to the raw label, so anything that needs the original string still has it.
- **736 files have no targets yet**, from the 98 uncovered panels. They are in the manifest and counted in totals, so a coverage-sensitive comparison should exclude them or report them separately. `explorer_data/coverage_report.json` lists them.

## Regenerating

```bash
uv run 3_1_build_explorer_data.py --offline
uv run summary_stats.py
```

The manifest comes from BigQuery on every run, so corpus counts track the current release. Marker counts change only when the curation is rerun or extended.
