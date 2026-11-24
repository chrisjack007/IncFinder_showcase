# Data dictionary and counting units

The data files use UTF-8, tab separators and a header. Each table is gzip-compressed. Empty fields indicate an inapplicable value or an empty source field. SHA-256 values use lowercase hexadecimal. Frozen and replay views share the same table schema.

| File | Unit of observation | Keys and interpretation |
|---|---|---|
| accessions.tsv.gz | One plasmid record in the GenBank snapshot | assembly_accession and sequence_accession form the composite key. Both include their version suffix. length_bp and sequence_sha256 describe the uppercase original sequence. gbff_sha256 identifies the compressed GenBank member. assembly_release_date comes from the frozen assembly catalog. inclusion records eligibility for the dated input. |
| clusters.tsv.gz | One dated plasmid | Join to accessions on its composite key. cluster_id and cluster_size identify the cluster within the view. representative_assembly and representative_accession point to the selected record. is_representative is 0 or 1. Across runs compare complete member sets. |
| representatives.tsv.gz | One complete representative plasmid | record_id is its versioned sequence accession. raw_rep_types preserves the replicon field of the MOB-suite report for this view. inc_subtypes contains the distinct extracted Inc subtypes. final_labels follows the rc2 column order. split is train or holdout. cv_fold ranges from 1 to 10 for training plasmids. |
| subtypes.tsv.gz | One historical subtype decision | raw_subtype, major_group, final_label and rule give the mapping and its rationale. major_plasmids and subtype_plasmids are distinct parent counts for that major group or subtype in the class-definition cohort. Subtypes can overlap on the same parent. |
| train_records.tsv.gz and holdout_records.tsv.gz | One model input record | record_id identifies the fragment or complete short plasmid. row_index is zero-based within each split. parent_accession and assembly_accession link to the representative and source. final_labels and cv_fold are inherited. length_bp and sequence_sha256 describe the normalized sequence used by the model. |
| labels.tsv.gz | One final category, with a separate No_Inc row | stage2_index is the zero-based column of the 21-dimensional stage-two vector and is empty for No_Inc. representative_plasmids counts distinct labeled parents. training_records and holdout_records count labeled inputs within the two splits. raw_subtypes names the members of each category. |
| sources.json and manifest.json | One data view | The first records input or run identities. The second records each member's byte count and SHA-256. A signed release manifest binds the downloadable assets. |

For metasim_fragment_1000_10000, source_batch_id identifies the batch. source_segments stores the pieces in concatenation order, with zero-based, half-open coordinates. fw denotes forward orientation and bw denotes a reverse complement before joining. A read crossing the circular origin retains its individual segments. source_strands lists the orientations present. The historical record type records the simulation target. Observed fragments can be shorter than 1,000 bp.

For full_plasmid_lt1000, the record covers its entire parent. Batch, interval and orientation fields are empty. Original GenBank sequences can include ambiguous bases. Model inputs normalize characters outside A, C, G, T and N to N. Distinguish the original sequence hash from the normalized input hash.

Records marked assembly_absent_from_frozen_date_catalog have no assembly date, cluster or model input. No_Inc means the study's MOB-suite replicon rule assigned no Inc subtype and supplies the negative class for stage one. The raw replicon field retains its order, duplicates and non-Inc names.

Counts within each label use distinct parent or record IDs. Multi-label membership can make sums across labels exceed the cohort size. The train and holdout partitions have disjoint parent plasmids. The model card documents exact sequence overlap across the two partitions.


For a whole short plasmid the trace also returns a derived interval from 0 through the parent length, in the stored forward orientation. For sampled fragments it returns the recorded intervals in concatenation order. Both use zero-based, half-open endpoints.

## The 19 original record metadata fields

train_source_metadata.tsv.gz and holdout_source_metadata.tsv.gz retain one row per original model record. Join each table to its model records by record_id.

| Historical fields | Meaning and missing values |
|---|---|
| `record_id`, `record_type`, `split` | Unique model record ID, how the record was generated, and the historical split name train or test. The model tables call test the holdout. |
| `plasmid_id`, `original_header`, `original_length_bp` | Versioned parent sequence accession, complete original representative FASTA header, and parent length in bp. |
| `sequence_length`, `n_count`, `ambiguous_replaced_count` | Length of the model sequence in bp, total N count after normalization, and the number of other ambiguous characters converted to N. |
| `source_status` | metasim_sampled_ge1000_plasmid denotes a simulated record from a parent of at least 1,000 bp. not_sampled_lt1000_plasmid_included_full_length denotes a complete short parent. |
| `source_gi`, `source_ref`, `metasim_read_id` | GI and reference identifiers assigned for simulator input, and the within-batch read ID. Empty on complete short records. |
| `raw_metasim_header` | Complete original simulator read header. Empty on complete short records. |
| `source_batch_id`, `source_fna` | Simulation batch and output file. Both views express the original machine-specific path relative to the batch and seed, retaining the filename. Empty on complete short records. |
| `source_segments`, `source_strands` | Oriented intervals in concatenation order, and the set of orientations in the read. Coordinates are zero-based and half-open. Empty on complete short records. |
| `original_sequence_sha256` | Present for complete short plasmids. Digest of the full normalized sequence, equal to the corresponding model record’s sequence_sha256. |

## Search and simulation evidence

In the comparison bundle, frozen-directed-rows.tsv.gz identifies each qualifying directed query/reference pair as assembly accession and sequence accession joined by a vertical bar. The remaining columns preserve the reported ANI, reference AF and query AF in percent. Each query's fingerprint includes repeated rows and all three reported values. An absent query in the fingerprint table has zero qualifying non-self rows. The edge comparison specifies the replay rows and fingerprints. verify-edges rebuilds the connected components including isolated nodes.

metasim-batches.tsv records each batch's split, seed, row count, ordered input digest, historical output digest and two independently run output digests. metasim-environment.json gives the launcher, JAR and Java identities observed during replay. typing-differences.tsv.gz compares the historical raw field, the rc2 subtype and label sets, and the new calls. A differences file containing only a header means that the corresponding comparison found no differing records.
