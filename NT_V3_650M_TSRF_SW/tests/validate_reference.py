"""Check fresh embeddings and RF decisions against frozen benchmark rows."""
from pathlib import Path
import argparse
import json
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from ntv3_tsrf.common import ROOT, read_json, write_json

parser=argparse.ArgumentParser()
parser.add_argument('--result', type=Path, required=True)
parser.add_argument('--frozen-result',type=Path)
parser.add_argument('--output',type=Path,required=True)
args=parser.parse_args()
fixture=ROOT/'tests/fixtures'
expected=np.load(fixture/'expected_rc2.npz',allow_pickle=False)
actual=np.load(args.result/'probabilities.npz',allow_pickle=False)
features=np.load(args.result/'features/features.npy',allow_pickle=False)
reference=np.load(fixture/'features.npy',allow_pickle=False)
tolerance=read_json(ROOT/'config/contract.json')['reference_tolerance']
checks={
    'embedding_allclose':bool(np.allclose(features,reference,atol=tolerance['embedding_atol'],rtol=tolerance['embedding_rtol'])),
    'embedding_shape_dtype': features.shape==reference.shape and features.dtype==reference.dtype,
    'gate_identical':bool(np.array_equal(actual['gate'],expected['gate'])),
    'labels_identical':bool(np.array_equal(actual['labels'],expected['labels'])),
    'stage2_na_mask_identical':bool(np.array_equal(np.isnan(actual['stage2']),np.isnan(expected['stage2']))),
}
if args.frozen_result:
    frozen=np.load(args.frozen_result/'probabilities.npz',allow_pickle=False)
    for name in ('stage1','stage2','labels','gate'):
        checks['frozen_'+name+'_exact']=bool(np.array_equal(frozen[name],expected[name],equal_nan=True))
report={'status':'PASS' if all(checks.values()) else 'FAIL','checks':checks,
        'reference_rows':read_json(fixture/'source_rows.json')['source_rows'],
        'embedding_max_abs_difference':float(np.max(np.abs(features-reference))),
        'stage1_max_abs_difference':float(np.max(np.abs(actual['stage1']-expected['stage1']))),
        'stage2_max_abs_difference':float(np.nanmax(np.abs(actual['stage2']-expected['stage2']))),
        'embedding_tolerance':tolerance,
        'scope':'selected frozen benchmark rows; not a new performance evaluation'}
write_json(args.output,report)
print(json.dumps(report,ensure_ascii=False,indent=2))
raise SystemExit(0 if report['status']=='PASS' else 1)
