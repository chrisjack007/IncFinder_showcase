# Where the 21 IncFinder rc2 Inc labels come from

[简体中文](README.zh-CN.md) · [Software home](../README.md) · [Provenance data release](https://github.com/chrisjack007/IncFinder_showcase/releases/tag/v0.5.0rc2-provenance.1) · [Data dictionary](FIELDS.md)

The 21 positive labels originate from replicon calls on complete representative plasmids. The study first dereplicated GenBank plasmids, typed each representative with MOB-suite and grouped its Inc subtypes. It then split the representative plasmids by date and sampled them with MetaSim. A fragment inherits its parent's reference label. Establishing whether that fragment contains a replicon locus requires separate evidence. [rc2 model contract](../NT_V3_650M_TSRF_SW/config/contract.json)

```mermaid
flowchart TD
    A["Frozen GenBank snapshot · 79,940 GBFF members"] --> B["Extract 97,904 plasmid records"]
    B --> C["97,898 records matched the assembly date catalog"]
    B --> X["6 records from assemblies absent in the date catalog"]
    C --> D["skani ANI and bidirectional AF → 45,911 clusters"]
    D --> E["One complete representative per cluster → MOB-suite replicon field"]
    E --> F["39 original Inc subtypes → 21 positive labels"]
    E --> N["35,247 No_Inc representatives"]
    F --> S["Split parents by assembly release date"]
    N --> S
    S --> T["Train · 36,527 parents"]
    S --> H["Holdout · 9,384 parents"]
    T --> R["17 MetaSim batches + 265 short plasmids → 800,265 training records"]
    H --> V["11 MetaSim batches + 95 short plasmids → 200,095 holdout records"]
```

## From GenBank to cluster representatives

The original record key combines a versioned assembly accession and a versioned sequence accession. The extraction rule accepted records with a plasmid qualifier in the source feature, or descriptions containing plasmid and excluding chromosome. The frozen date catalog contained 143,513 distinct assemblies. Six recovered records belonged to assemblies absent from it. Their rows are included in accessions.tsv.gz.

| Assembly | Sequence accessions excluded from dereplication |
|---|---|
| GCA_006228425.1 | CP030165.1 |
| GCA_029536795.1 | CP120578.1, CP120579.1, CP120580.1, CP120582.1, CP120583.1 |

The skani query list contained sequences of at least 500 bp. Clustering initialized a node for each of the 97,898 dated records. Distinct records were connected when ANI was ≥80% and the smaller of the reference and query alignment fractions was ≥90%. The historical total of 3,544,354 qualifying non-self search rows preserves search direction. Connected components yielded 45,911 clusters. Each cluster contributed the plasmid with the earliest assembly release date. Ties were resolved by greater sequence length, then historical FASTA path in lexical order. Compare complete member sets and representative accessions across runs. The numeric cluster IDs identify clusters within a view. [skani 0.3.1](https://github.com/bluenote-1577/skani/releases/tag/v0.3.1)

## From the replicon field to 21 classes

MOB-suite 3.1.9 typed the complete representative plasmids. Inc names were extracted from the replicon field, counting each distinct subtype once per plasmid. The table counts representatives over all 45,911 plasmids, including those later assigned to the holdout cohort. The support threshold was 100 distinct plasmids. Branch and rare-group decisions agreed on 2026-05-07 determined the final categories. The count for a merged label is the number of distinct parents carrying any member subtype. [Archived rules of 7 May 2026](POLICY-SOURCES.md) · [All 39 subtype decisions](subtype-policy.tsv) · [Field definitions](FIELDS.md) · [MOB-suite reference database](https://doi.org/10.5281/zenodo.10304948)

| Stage-two index | Label | Original Inc subtypes | Decision | Parents | Training records | Holdout records |
|---:|---|---|---|---:|---:|---:|
| 0 | Inc18 | Inc18 | Single observed subtype in a major group with at least 100 parents | 115 | 1,493 | 326 |
| 1 | IncC | IncC | Single observed subtype in a major group with at least 100 parents | 348 | 7,773 | 1,839 |
| 2 | IncFIA | IncFIA | Subtype covers at least 100 parents in a multi-subtype group | 2,816 | 37,464 | 10,565 |
| 3 | IncFIB | IncFIB | Subtype covers at least 100 parents in a multi-subtype group | 4,144 | 80,158 | 17,969 |
| 4 | IncFIC | IncFIC | Subtype covers at least 100 parents in a multi-subtype group | 1,361 | 19,530 | 5,290 |
| 5 | IncFII | IncFII | Subtype covers at least 100 parents in a multi-subtype group | 3,122 | 51,585 | 12,765 |
| 6 | IncHI1 | IncHI1A, IncHI1B | Both branch members merge into IncHI1 | 715 | 18,416 | 4,035 |
| 7 | IncHI2 | IncHI2A | Branch member grouped under its parent | 310 | 10,987 | 2,224 |
| 8 | IncI | IncI-gamma/K1, IncI1, IncI1/B/O, IncI2 | A preliminary group remained below 100, so the IncI major group was pooled | 567 | 7,122 | 1,509 |
| 9 | IncK | IncK2/Z | Single observed subtype in a major group with at least 100 parents | 171 | 2,248 | 620 |
| 10 | IncL | IncL/M | Single observed subtype in a major group with at least 100 parents | 114 | 1,335 | 217 |
| 11 | IncN | IncN, IncN2 | Retain IncN and pool the rare IncN2 subtype into it | 464 | 5,813 | 1,161 |
| 12 | IncP | IncP | Single observed subtype in a major group with at least 100 parents | 295 | 5,200 | 1,152 |
| 13 | IncQ | IncQ1, IncQ2 | A preliminary group remained below 100, so the IncQ major group was pooled | 290 | 5,698 | 813 |
| 14 | IncR | IncR | Single observed subtype in a major group with at least 100 parents | 1,000 | 11,932 | 3,322 |
| 15 | IncU | IncU | Single observed subtype in a major group with at least 100 parents | 436 | 8,121 | 2,604 |
| 16 | IncX1 | IncX1 | Subtype covers at least 100 parents in a multi-subtype group | 458 | 4,585 | 906 |
| 17 | IncX3 | IncX3 | Subtype covers at least 100 parents in a multi-subtype group | 151 | 1,510 | 590 |
| 18 | IncX_other | IncX2, IncX4, IncX5, IncX6, IncX7, IncX8, IncX9 | Pooled low-count IncX subtypes together covered at least 100 parents | 116 | 770 | 164 |
| 19 | IncY | IncY | Single observed subtype in a major group with at least 100 parents | 225 | 3,048 | 563 |
| 20 | Inc_other | Inc10, Inc11, Inc13, Inc4, IncA, IncT, IncW | Each original major group had fewer than 100 parents | 202 | 3,197 | 875 |
| — | No_Inc | — | No Inc subtype extracted | 35,247 | 631,608 | 161,382 |

No_Inc supplies the negative class for stage one. Of 10,664 positive representatives, 5,046 carried multiple final labels. A plasmid carrying both IncHI1A and IncHI1B counts once toward IncHI1. Stage-two indices are zero-based and follow the rc2 model contract. The ct and u05 modes govern prediction. The labels here are the parent plasmid reference annotations.

The historical rule uses two counting steps. First it counts distinct plasmids per subtype and major group. Within each major group it then sums subtype counts assigned to each preliminary label. A preliminary total below 100 triggers the whole-major merge for eligible letter groups. The published parent count for a final label deduplicates plasmids again. IncHI1A occurred on 44 representatives and IncHI1B on 672. Their preliminary support was 716. One representative carried both, giving 715 distinct IncHI1 representatives.

## From a representative to a model input

Plasmids with assembly release dates through 2024 entered training. Those released from 2025 onward entered the holdout. Training folds were assigned by parent. MetaSim 0.9.5 generated 800,000 training and 200,000 holdout fragments. Adding 265 and 95 complete plasmids shorter than 1,000 bp brought the totals to 800,265 and 200,095 records. Observed lengths span 200–10,000 bp in training and 212–10,000 bp in holdout. The model inputs draw on 35,893 distinct training parents and 9,236 distinct holdout parents, with no parent in both. The model card documents one holdout sequence identical to four training sequences. [Model card](../docs/MODEL_CARD.en.md) · [rc2 training input and output digests](../NT_V3_650M_TSRF_SW/config/research_index.json)

The ten training folds follow the archived representative-level assignments. The frozen export checked those assignments against every row of the rc2 training-fold array and checked inherited labels against both label matrices. In the replay, the same representative accession carries its fixed fold. Holdout records have an empty fold.

## Three records to trace

| Example | Fragment (length) | How its label was assigned | Parent plasmid (GenBank) |
|---|---|---|---|
| Multiple labels | test_frag_000045001, 2,800 bp | The original field contains IncFIB twice and IncFII once. The fragment inherits IncFIB and IncFII. | [OZ180215.1](https://www.ncbi.nlm.nih.gov/nuccore/OZ180215.1) |
| Branch merge | test_frag_000129761, 1,025 bp | The parent carries both IncHI1A and IncHI1B. They contribute one IncHI1 label. | [JBREQR010000002.1](https://www.ncbi.nlm.nih.gov/nuccore/JBREQR010000002.1) |
| No_Inc | test_frag_000000001, 889 bp | The replicon field is a dash. The fragment crosses its parent's circular origin and inherits No_Inc. | [JBKHUK010000010.1](https://www.ncbi.nlm.nih.gov/nuccore/JBKHUK010000010.1) |

Download the frozen archive and query tool from the data release. Run these commands from the directory containing the extracted frozen folder with Python 3.10 or later. On Windows, py -3 also invokes Python 3. Each record lookup reports links to the versioned NCBI sequence and assembly.

```bash
python incfinder_provenance.py verify
python incfinder_provenance.py trace --record test_frag_000045001
python incfinder_provenance.py trace --record test_frag_000129761
python incfinder_provenance.py trace --record test_frag_000000001
python incfinder_provenance.py trace --label IncHI1
```

The default data directory is frozen. Add --data replay to query the recomputed view. The [replay guide](REPLAY.md) covers release assets, signature verification, sequence retrieval, stage entry points and the differences between views.
