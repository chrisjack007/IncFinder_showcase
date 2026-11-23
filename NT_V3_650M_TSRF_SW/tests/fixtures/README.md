# Frozen reference test data / 冻结参考测试数据

## Identity and source / 身份与来源

The five immutable scientific payloads contain 11 sequences and total 102,462 bytes. This README is documentation and is excluded from that payload count. All payloads are ordinary files, not links. They originate from the fixed embedding benchmark input identified below; this is a selected engineering set, not a random or representative accuracy-evaluation cohort.

- Benchmark input ID: dce1d70ee87ecdd593f67d43d71984fd6ee0e5df1cdb6a52ab5d4f48b93328ac
- Feature/row provenance: source_rows.json, with zero-based source row indices.
- Parent feature SHA-256: 0d9b59a317f5f27555e6353a5d3bedbf898481e23877b92de0ee0fdf0dd11a38
- Encoding/model contract: ../../config/contract.json and ../../config/assets.json.

| Payload | Bytes | SHA-256 |
|---|---:|---|
| expected.npz | 2204 | d0bbb31faf9b7a6fee8c13e9b271a0a75dc01bae08853580e5f0c792f68fb03d |
| features.json | 3572 | 7c55087a8cef6083a55c70fbf03dcb8020f5ee41f5afa4bdad6f6afdf478dbbc |
| features.npy | 67712 | 5f8bc5a7a46f6167c3e1e3db20808ed2572cfe84a786a32691879c43df69e96f |
| reference.fasta | 28357 | d77da8445d8ea189b9b64cc827e601ca6e29ba22401711a55b215ca97c29b336 |
| source_rows.json | 617 | 4f8f256dc34761d0b1cda442b23c06ca09d8da76ced2085ddc11a7d7fc7fcb13 |

## Structure and data flow / 结构与上下游

```text
reference.fasta       11 sequences in authoritative record order
features.npy          frozen (11, 1536) float32 feature matrix
features.json         dimensions, dtype, row IDs, sequence hashes and contract binding
expected.npz          frozen expected RF probabilities, gates and calls
source_rows.json      source row indices, lengths and expected state counts
```

reference.fasta is parsed into records; features.json binds each row to the exact ID, order, input format and sequence SHA-256. features.npy is consumed by the two frozen RFs. expected.npz is the regression target. source_rows.json records upstream selection; no original workstation/server path is needed to execute these tests.

Array axes and meanings:

| Array | Shape | dtype | Meaning |
|---|---|---|---|
| features.npy | (11,1536) | float32 | rows follow reference.fasta; columns are encoder dimensions |
| expected.npz:stage1 | (11,) | float32 | Stage-1 positive-class probabilities |
| expected.npz:stage2 | (11,21) | float32 | columns follow contract.json labels; rejected rows are NaN |
| expected.npz:gate | (11,) | bool | Stage-1 probability >= 0.5 |
| expected.npz:labels | (11,21) | uint8 | 0/1 calls under the frozen ct thresholds |

Sequences contain 28,087 bases in total and span 212–10,000 bp. Under ct, native counts are 5 Inc_called, 5 Unclassified (gate rejection), and 1 Inc_no_call. External Unclassified is 6; under u05 it is 8 (5 rejected + 3 no-call). The gate passes 6 records in both modes. V1 status names are historical; V2 keeps native states distinct while merging their public no-call result.

examples/sample.fasta and examples/sample.fastq, two levels above this directory, contain the same first two reference sequences in matching order. They demonstrate input formats, not accuracy.

## Modification rules / 修改规范

Do not edit, reorder, truncate, or regenerate these scientific payloads to make a failing test pass. No field is intended for routine user editing. New test inputs must be separate from this fixed set. A scientific payload change requires a new explicitly reviewed contract and synchronized identities, expected outputs, provenance and validation; it is not part of this release.

Documentation may clarify semantics while retaining the original payload identities. Preserve complete raw probabilities, including NaN masks. Labels follow the fixed contract order, not alphabetical reordering.

## Executable acceptance / 可执行验收

From the software root in its documented environment:

```bash
python -B -m unittest discover -s tests -p 'test_*.py' -v
test_root=$(mktemp -d)
incfinder predict tests/fixtures/reference.fasta -o "$test_root/frozen"   --features-dir tests/fixtures --model-dir models
python - "$test_root/frozen/probabilities.npz" <<'PY'
import sys
import numpy as np
expected = np.load("tests/fixtures/expected.npz", allow_pickle=False)
actual = np.load(sys.argv[1], allow_pickle=False)
for key in ("stage1", "stage2", "gate", "labels"):
    assert np.array_equal(actual[key], expected[key], equal_nan=True), key
print("Frozen RF reference: PASS")
PY
```

Outputs remain under the system temporary directory. New-sequence encoding needs the research-preview flag and is evaluated separately; agreement on these 11 labels does not establish original-embedding equivalence or population accuracy.

## V2 derived output reference

expected_output_v2.json is an additional derived reference, excluded from the five-file / 102,462-byte immutable payload total. It records original payload hashes and independently computes ct/u05 calls, external flags and internal states from the original frozen probabilities. It does not replace expected.npz or alter original inputs. See ../../docs/output_policy.md.

## Independent 0.5.0rc2 RF reference

The installed candidate regression uses expected_rc2.npz and expected_output_rc2.json. The original V2 probability fixture remains available for historical tests. Features and row identities are shared. The separate public research index identifies the real training jobs.
