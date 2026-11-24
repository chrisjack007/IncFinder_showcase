# Verifying the release and replaying each stage

[简体中文](REPLAY.zh-CN.md) · [Label provenance](README.md) · [Data release](https://github.com/chrisjack007/IncFinder_showcase/releases/tag/v0.5.0rc2-provenance.1)

## Download, authenticate and verify

Run the following Bash commands in an empty directory. The query tool uses the Python 3.10+ standard library. The signing key is pinned to the original software tag. The signed digest manifest lists the frozen, replay, comparison, controls and tools bundles separately.

```bash
base=https://github.com/chrisjack007/IncFinder_showcase/releases/download/v0.5.0rc2-provenance.1
prefix=IncFinder-0.5.0rc2-provenance.1
for section in frozen replay controls comparison tools; do
  curl -fL --retry 3 -O "$base/$prefix-$section.tar.gz"
done
for file in PROVENANCE-SHA256SUMS PROVENANCE-SHA256SUMS.sig incfinder_provenance.py; do
  curl -fL --retry 3 -O "$base/$file"
done
curl -fL --retry 3 -o allowed_signers https://raw.githubusercontent.com/chrisjack007/IncFinder_showcase/v0.5.0rc2/licenses/incfinder-release-allowed-signers
ssh-keygen -Y verify -f allowed_signers -I incfinder-release -n incfinder-release -s PROVENANCE-SHA256SUMS.sig < PROVENANCE-SHA256SUMS &&
  sha256sum --check PROVENANCE-SHA256SUMS
```

After both the signature and file digests pass, extract and verify the two views. Verification reselects cluster representatives, counts subtype and class memberships, checks every record's parent, intervals, labels and training fold, and tests for parent overlap across splits. For files already downloaded, `python incfinder_provenance.py verify-release . --signer allowed_signers` also verifies the signed assets.

```bash
prefix=IncFinder-0.5.0rc2-provenance.1
for section in frozen replay controls comparison tools; do
  tar -xzf "$prefix-$section.tar.gz"
done
python incfinder_provenance.py verify --data frozen
python incfinder_provenance.py verify --data replay
python incfinder_provenance.py verify-edges --data frozen \
  --rows comparison/frozen-directed-rows.tsv.gz --index comparison/frozen-skani-edges.tsv.gz
```

## Full replay and differences

| Stage | Frozen | Full replay | Row-level comparison |
|---|---:|---:|---|
| GenBank extraction | 97,904 plasmid records | 97,904 | All versioned accessions, lengths and sequence SHA-256 values match. |
| skani search | 3,544,354 qualifying directed rows | 3,544,354 | Queries with different endpoints: 0. Queries with any endpoint, ANI or AF difference: 0. |
| skani clustering | 45,911 components | 45,911 | 0 plasmids changed membership sets and 0 changed representative. |
| MOB-suite on complete representatives | 45,911 plasmids | 45,911 | Raw replicon text, subtype sets and final labels differ from the historical merged report on 0, 0 and 0 representatives respectively. Against rc2's adopted table, 0 subtype sets and 0 final labels differ. |
| Subtype policy | 39 subtypes, 21 positive classes | 39 subtypes, 21 positive classes | 0 subtype rows differ in mapping, rationale or support. 0 final-category summary rows differ. |
| MetaSim and complete short plasmids | 800,265 training and 200,095 holdout records | 800,265 training and 200,095 holdout | 0 training and 0 holdout records have a differing field. |

Both views preserve the 19 historical record-metadata fields. After expressing the run-specific source filename relative to its batch and seed, 0 training and 0 holdout metadata rows differ. The 360 complete short plasmids have no simulation output path.

The linked replay rebuilt both representative FASTAs and all 28 ordered batch inputs from the recovered GenBank sequences, checked their SHA-256 values and ran licensed MetaSim. A separate set of runs used the frozen stage inputs. The GX2 run recorded 479 MOB-suite batches and 196 skani search partitions. The comparison bundle includes each qualifying directed pair with ANI and both AF values, plus the queries, representatives, records and fields affected by differences. Work directories change between machines. Comparisons use versioned accessions and the numerical precision of the original reports.

No stage had a row-level divergence.

The two independently generated directed comparison tables have the same uncompressed SHA-256, `533fd7cfaf52e2d0f0adc1c238681a32bf269eb720d02455ea579a8d7204c284`. The comparison bundle stores one copy of those identical rows and a separate query index for each run.

An independent check reconstructed all 1,000,360 model sequences from the recorded intervals and orientations on the recovered parents. Every sequence digest matched the frozen record, including 155,924 records with multiple intervals. The comparison bundle records both cohorts in source-coordinate-checks.json.

The original replicon text from the historical MOB-suite merged report is in frozen/representatives.tsv.gz. The three-column typing table used by rc2 data preparation is in controls/rc2-typing.tsv.gz. The stock mob_typer results of this replay are in replay/representatives.tsv.gz. The archived reconciliation recorded 17,929 in its working-spreadsheet rep_type_mismatch field, 0 differences in extracted Inc subtypes and 0 differences in final labels. The present check independently re-extracted Inc subtypes on all 45,911 spreadsheet rows. The raw-text comparison above uses the historical merged report and fresh stock output.

## Obtaining original source material

controls/source_records.tsv.gz lists the versioned assembly and sequence accessions, sequence digests and compressed-member identity for each source plasmid. controls/archive_members.tsv.gz enumerates the snapshot's 79,940 genomic.gbff.gz members. controls/catalog.tsv.gz lists assembly accessions and dates from the study catalog. Follow the NCBI links in a trace to inspect one record or retrieve GenBank data by versioned assembly accession. Current distributions can differ in gzip encoding or annotation version. Check the member and sequence digests. [NCBI Datasets downloads](https://www.ncbi.nlm.nih.gov/datasets/docs/v2/how-tos/genomes/download-genome/)

The original tar is 221,691,648,000 bytes. The historical package checksum for the GCA_022558465.1 member was reconciled separately against an official gzip source. The member index preserves the original MD5, observed MD5 and verification basis. For separately obtained members, pass the root containing the archive_member relative paths to genbank with `--member-index controls/archive_members.tsv.gz`. If only the compression container differs, `--allow-repacked-members` records each changed container and still requires identical plasmid accessions, lengths and sequence digests. This mode establishes extracted-record equivalence.

For a fresh retrieval, use the NCBI Datasets CLI in the extracted release directory. The index supplies the versioned assembly accessions. The public service serves currently available GBFF files for those accessions. A retired assembly, revised annotation that changes the recovered plasmid inventory, or missing member will stop the check. This release was tested against the complete historical tar. The directory mode was separately tested with identical, repacked and altered records. [NCBI large genome downloads](https://www.ncbi.nlm.nih.gov/datasets/docs/v2/how-tos/genomes/large-download/) · [NCBI genome package files](https://www.ncbi.nlm.nih.gov/datasets/docs/v2/reference-docs/data-packages/genome/)

```bash
gzip -cd controls/archive_members.tsv.gz | awk -F '\t' 'NR > 1 {print $1}' > snapshot-accessions.txt
datasets download genome accession --inputfile snapshot-accessions.txt --include gbff --dehydrated --filename source1.zip
unzip -q source1.zip -d source1_bacteria_genbank_complete
datasets rehydrate --directory source1_bacteria_genbank_complete
find source1_bacteria_genbank_complete/ncbi_dataset/data -type f -name genomic.gbff -exec gzip -nk {} +
python incfinder_provenance.py replay genbank . controls/source_records.tsv.gz work/restored-shards \
  --member-index controls/archive_members.tsv.gz --allow-repacked-members
```

The historical MOB-suite job recorded software version 3.1.9. Its original database content digest was not recorded. This replay uses the rc2-verified database. controls/mob-database.json lists the SHA-256 and size of each of the 44 members. The replicon reference rep.dna.fas matches the DOI version. The entire database identity is checked separately. [MOB-suite](https://github.com/phac-nml/mob-suite) · [Database release](https://doi.org/10.5281/zenodo.10304948)

Run a licensed local copy of MetaSim 0.9.5. The release provides the seeds, bins, input order, original output digests and replay environment digests. Original logs record neither the executable hash nor the Java version. The licensed installer remains in its execution environment. [MetaSim paper](https://doi.org/10.1371/journal.pone.0003373)

## Stage commands

In the Linux commands below, SNAPSHOT is the local frozen tar, SKANI, MOB_TYPER and METASIM are absolute paths to installed executables, and MOB_DB is the reference directory configured below. Use a fresh work directory. The MetaSim commands also run in WSL2.

```bash
python incfinder_provenance.py replay genbank "$SNAPSHOT" controls/source_records.tsv.gz work/shards \
  --archive-sha256 aa88e7242d488326ce2900c3bb289f2ae290e2fee227504c21c622c1c01d3845
python incfinder_provenance.py replay skani --program "$SKANI" --shards work/shards \
  --catalog controls/catalog.tsv.gz --dated controls/dated.tsv.gz --inventory controls/source_records.tsv.gz \
  --membership controls/membership.tsv.gz --output work/skani --threads 32 --profile controls/skani-profile.json
```

The skani graph starts with all dated records. Search used a 500 bp lower limit and batches of 500 queries. The replay uses the GX2 skani 0.3.1 executable, Bioconda build ha6fb395_0. The Conda installation record on 2026-04-22 and the named environment in the 2026-04-23 job script support this version identification. Two original binary headers agree on compression 125, k = 15, marker compression 1,000, nucleotide mode and syncmers off. The original index has exactly the 97,750 query records at least 500 bp long. The other 148 dated nodes remain in the graph. The restored query order matches the date ledger. [skani 0.3.1](https://github.com/bluenote-1577/skani/releases/tag/v0.3.1)

MOB-suite 3.1.9 initializes a fresh directory from the upstream Zenodo database v. 3.1.8, released on 8 December 2023. We read the complete 472,851,578-byte DOI archive, which has SHA-256 531f2124269b13e6fe59d0b01936276b5a47c0c0eebbc787f710cd376f77d3cd. All eight original data files matched the rc2 snapshot. The command below checks each of them, records the actual inventory of the entire installed directory and compares the resulting calls. To check the exact 44-file GX2 snapshot, point MOB_DB to that directory and omit the initialization command and --upstream-reference. Locally built indices, run markers and the taxonomy cache can have different digests. A separate initialization used the verified DOI archive cache and a recorded NCBI taxonomy snapshot. It produced 43 installed files. Raw replicon fields, subtype sets and final labels matched GX2 on four control plasmids. The full 45,911-plasmid replay used the 44-file GX2 snapshot. The comparison bundle lists the observed directory differences in initialized-reference-control.json. The database record credits Robertson and specifies CC BY 4.0. [Zenodo](https://doi.org/10.5281/zenodo.10304948) · [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) · [MOB-suite 3.1.9](https://github.com/phac-nml/mob-suite/blob/3.1.9/mob_suite/config.json)

```bash
export PATH="$(dirname "$MOB_TYPER"):$PATH"
MOB_DB="$PWD/work/mob-db"
mkdir -p work/mob-init
(cd work/mob-init && mob_init -d "$MOB_DB")
```

```bash
python incfinder_provenance.py replay inputs controls work/inputs --fasta-dir work/skani/fasta
python incfinder_provenance.py replay mob \
  --fasta work/inputs/00_input/train.plasmids.ANI80_AF90.fa work/inputs/00_input/test.plasmids.ANI80_AF90.fa \
  --program "$MOB_TYPER" --database "$MOB_DB" --database-manifest controls/mob-database.json \
  --upstream-reference controls/mob-upstream-reference.json \
  --policy controls/category_policy.tsv --reference controls/rc2-typing.tsv.gz --output work/mob
python incfinder_provenance.py replay rules work/mob/typing.tsv work/recomputed-policy.tsv
```

inputs reconstructs the two representative FASTAs and all 28 batch FASTAs in frozen order, checking their 30 file digests. Each batch retains its actual post-deduplication input order. The duplicate_sequence_aliases tables identify another 221 training and 34 holdout sequence aliases. mob validates the reference according to the selected profile before running stock mob_typer on independent plasmids. It compares grouped and individual calls on control representatives. rules counts the new raw fields and applies the historical inclusion and pooling decisions.

With the two rebuilt representative FASTAs, the verifier extracts the reported intervals in the specified orientations, concatenates circular segments in order and recalculates the sequence SHA-256 for each model record, including complete short plasmids.

```bash
python incfinder_provenance.py verify --data frozen --representative-inputs work/inputs
```

```bash
for split in train test; do
  python incfinder_provenance.py replay metasim work/inputs work/metasim --metasim "$METASIM" --split "$split" || exit 1
done
python incfinder_provenance.py replay assemble work/inputs work/metasim work/final
python incfinder_provenance.py replay reconcile \
  --frozen frozen --controls controls --genbank work/shards --skani work/skani \
  --mob work/mob --assembled work/final --output work/replay --comparison work/comparison
python incfinder_provenance.py verify --data work/replay
```

assemble preserves batch and read order, appends complete plasmids shorter than 1,000 bp in representative-FASTA order and normalizes the bases. reconcile derives the policy from the new raw fields, joins the actual cluster and typing outputs, and hashes each newly generated sequence. It compares parent, intervals, orientation, length, split, fold and inherited labels record by record. For stage controls, use the frozen input to each stage. A changed upstream representative set also requires downstream results on that new cohort, alongside the frozen-input control.
