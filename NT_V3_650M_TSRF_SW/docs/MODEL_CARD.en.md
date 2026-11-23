# IncFinder 0.5.0rc2 research preview model card

IncFinder assigns 21 candidate incompatibility labels per sequenced FASTA/FASTQ record. Training lengths were 200–10,000 bp. Stage 1 gates at 0.5. Rejected records remain in the evaluation denominator and have NA stage-2 probabilities. The default ct selects label thresholds from training out-of-fold predictions. The u05 mode uses 0.5 throughout.

The holdout has 200,095 records in 9,236 plasmid groups disjoint from training. It has already informed version comparisons. One original holdout sequence is identical to four training records. After excluding the holdout row, both F1 scores are unchanged at three decimals in both modes.

| Mode | Precision (micro) | Recall (micro) | Micro-F1 (95% CI) | Macro-F1 (95% CI) |
|---|---:|---:|---:|---:|
| ct | 54.87% | 61.21% | 0.579 (0.568–0.589) | 0.445 (0.429–0.460) |
| u05 | 69.83% | 41.54% | 0.521 (0.509–0.532) | 0.351 (0.336–0.367) |

Percentile intervals resample whole plasmid groups 1,000 times with seed 20261123. Both modes detect only 1 of 326 reference-positive Inc18 records. A negative result cannot exclude Inc18. Raw-sequence embeddings vary between platforms and require `--allow-encoder-drift`.

The earlier version-comparison readout reported group-bootstrap intervals of 0.569–0.589 and 0.429–0.461 for ct micro- and macro-F1, and 0.510–0.532 and 0.336–0.366 for u05. The table above reports an independently seeded resampling. The point estimates agree.

| Inc | Ref + | ct precision | ct recall | ct F1 | u05 precision | u05 recall | u05 F1 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Inc18 | 326 | 0.500 | 0.003 | 0.006 | 0.500 | 0.003 | 0.006 |
| IncC | 1839 | 0.771 | 0.484 | 0.595 | 0.861 | 0.446 | 0.588 |
| IncFIA | 10565 | 0.633 | 0.632 | 0.632 | 0.795 | 0.445 | 0.571 |
| IncFIB | 17969 | 0.574 | 0.814 | 0.673 | 0.636 | 0.712 | 0.672 |
| IncFIC | 5290 | 0.449 | 0.669 | 0.537 | 0.627 | 0.193 | 0.295 |
| IncFII | 12765 | 0.514 | 0.668 | 0.581 | 0.693 | 0.400 | 0.507 |
| IncHI1 | 4035 | 0.535 | 0.598 | 0.565 | 0.711 | 0.282 | 0.404 |
| IncHI2 | 2224 | 0.769 | 0.592 | 0.669 | 0.882 | 0.479 | 0.621 |
| IncI | 1509 | 0.587 | 0.478 | 0.527 | 0.841 | 0.315 | 0.458 |
| IncK | 620 | 0.661 | 0.465 | 0.545 | 0.865 | 0.290 | 0.435 |
| IncL | 217 | 0.699 | 0.502 | 0.584 | 0.873 | 0.318 | 0.466 |
| IncN | 1161 | 0.571 | 0.281 | 0.376 | 0.792 | 0.194 | 0.311 |
| IncP | 1152 | 0.641 | 0.172 | 0.271 | 0.833 | 0.113 | 0.199 |
| IncQ | 813 | 0.241 | 0.157 | 0.190 | 0.893 | 0.092 | 0.167 |
| IncR | 3322 | 0.325 | 0.295 | 0.309 | 0.885 | 0.085 | 0.156 |
| IncU | 2604 | 0.547 | 0.336 | 0.417 | 0.912 | 0.107 | 0.192 |
| IncX1 | 906 | 0.507 | 0.301 | 0.378 | 0.936 | 0.178 | 0.299 |
| IncX3 | 590 | 0.652 | 0.283 | 0.395 | 0.976 | 0.139 | 0.243 |
| IncX_other | 164 | 0.482 | 0.250 | 0.329 | 0.852 | 0.140 | 0.241 |
| IncY | 563 | 0.330 | 0.410 | 0.366 | 0.566 | 0.099 | 0.169 |
| Inc_other | 875 | 0.957 | 0.255 | 0.403 | 0.990 | 0.225 | 0.367 |

The training runs, input identities, RF digests and threshold identities are recorded in [research_index.json](../config/research_index.json).
