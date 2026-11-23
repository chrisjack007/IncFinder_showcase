"""Frozen NTv3 FP32 encoding; no remote code resolution or downloads."""
import argparse
from contextlib import nullcontext

from .checkpoints import FEATURE_WIDTH, encoding_runtime, managed_checkpoints, plan_index_batches
from .common import ROOT, environment, sha256
from .encoder_source import encoder_classes
from .feature_metadata import write_feature_metadata
from .record_index import indexed_records
from .resources import model_root
from .sequence_io import reverse_complement


def _encode_batch(records, batch, model, tokenizer, torch, np, device):
    selected, padded_length = batch.rows, batch.padded_length
    pooled_strands = []
    for reverse in (False, True):
        sequences = [reverse_complement(records[i].sequence) if reverse else records[i].sequence for i in selected]
        lengths = torch.tensor([len(s) for s in sequences], device=device)
        padded = [s + 'N' * (padded_length - len(s)) for s in sequences]
        tokens = tokenizer(padded, add_special_tokens=False, padding=False, return_tensors='pt')['input_ids'].to(device)
        if tuple(tokens.shape) != (len(selected), padded_length):
            raise ValueError('The tokenizer does not encode one token per base')
        with torch.inference_mode():
            output = model(input_ids=tokens, output_hidden_states=True, output_attentions=False, return_dict=True)
            hidden = output.hidden_states[-1]
            if tuple(hidden.shape) != (len(selected), padded_length, FEATURE_WIDTH):
                raise ValueError(f'Unexpected hidden-state shape: {tuple(hidden.shape)}')
            mask = torch.arange(padded_length, device=device)[None, :] < lengths[:, None]
            pooled = (hidden.float() * mask.unsqueeze(-1)).sum(1, dtype=torch.float32) / lengths[:, None].float()
            pooled_strands.append(pooled.cpu().numpy().astype(np.float32))
        del output, hidden, tokens, pooled
    averaged = (pooled_strands[0] + pooled_strands[1]) / np.float32(2)
    if not np.isfinite(averaged).all():
        raise ValueError('Embeddings contain NaN/Inf')
    return averaged


def encode(input_path, output_dir, device='cpu', batch_size=1, threads=4, model_dir=None, *, run_id=None,
           index_path=None, index_sha256=None):
    with indexed_records(input_path, index_path=index_path, index_sha256=index_sha256) as records:
        with encoder_classes(ROOT, model_dir) as classes:
            _encode(input_path, output_dir, records, device, batch_size, threads, model_dir, run_id, classes)


def _encode(input_path, output_dir, records, device, batch_size, threads, model_dir, run_id, classes):
    versions = environment('embedding')
    import numpy as np
    import torch
    Ntv3PreTrainedConfig, NTv3PreTrained, NTv3Tokenizer = classes
    if batch_size < 1 or threads < 1:
        raise ValueError('batch_size and threads must be positive integers')
    torch.set_num_threads(threads)
    if device == 'cuda' and not torch.cuda.is_available():
        raise ValueError('CUDA was requested but is unavailable in the current environment')
    torch.manual_seed(42)
    if device == 'cuda':
        torch.backends.cuda.matmul.allow_tf32 = False
        # Match the original encoder's torch 2.11 CUDA backend defaults.
        # FP32 tensors do not imply that cuDNN convolution TF32 is disabled.
        torch.backends.cudnn.allow_tf32 = True
        torch.backends.cudnn.benchmark = False
    model_dir = model_root(model_dir) / 'ntv3'
    checkpoint_context = nullcontext()
    if run_id is not None:
        checkpoint_context = managed_checkpoints(
            run_id, input_path, records.identities(), device=device, batch_size=batch_size, threads=threads,
            runtime=encoding_runtime(versions, torch, device),
        )
    with checkpoint_context as checkpoints:
        config = Ntv3PreTrainedConfig.from_pretrained(model_dir, local_files_only=True)
        tokenizer = NTv3Tokenizer.from_pretrained(model_dir, local_files_only=True)
        model = NTv3PreTrained.from_pretrained(model_dir, config=config, local_files_only=True,
                                            dtype=torch.float32).to(device).eval()
        if next(model.parameters()).dtype != torch.float32:
            raise ValueError('NTv3 weights must use FP32')
        output_dir.mkdir(parents=True, exist_ok=True)
        destination = output_dir / 'features.npy'
        values = (np.lib.format.open_memmap(destination, mode='w+', dtype=np.float32, shape=(len(records), FEATURE_WIDTH))
                  if checkpoints is None else None)
        try:
            for batch in plan_index_batches(records, batch_size):
                if checkpoints is not None and checkpoints.reuse(batch):
                    print(f'Reused encoding batch {batch.id}', flush=True)
                    continue
                averaged = _encode_batch(records, batch, model, tokenizer, torch, np, device)
                if checkpoints is None:
                    values[list(batch.rows)] = averaged
                else:
                    checkpoints.save(batch, averaged)
                print(f'Encoded {len(batch.rows)} rows at padded_length={batch.padded_length}', flush=True)
            if checkpoints is None:
                values.flush()
            else:
                checkpoints.assemble(plan_index_batches(records, batch_size), len(records), destination)
        finally:
            del values
        write_feature_metadata(output_dir / 'features.json', {
            'schema': 1, 'shape': [len(records), FEATURE_WIDTH], 'dtype': 'float32',
            'features_sha256': sha256(destination),
            'contract_sha256': sha256(ROOT / 'config/contract.json'),
            'assets_sha256': sha256(ROOT / 'config/assets.json'),
            'environment': versions, 'device': device,
            'scientific_equivalence': 'NOT_ESTABLISHED',
            'torch_build': torch.__version__, 'cuda_runtime': torch.version.cuda,
            'gpu_name': torch.cuda.get_device_name(0) if device == 'cuda' else None,
            'batch_size': batch_size, 'threads': threads,
            'cuda_matmul_allow_tf32': bool(torch.backends.cuda.matmul.allow_tf32) if device == 'cuda' else None,
            'cudnn_allow_tf32': bool(torch.backends.cudnn.allow_tf32) if device == 'cuda' else None,
        }, records.identities())


if __name__ == '__main__':
    from pathlib import Path
    parser = argparse.ArgumentParser()
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--device', choices=['cpu', 'cuda'], default='cpu')
    parser.add_argument('--batch-size', type=int, default=1)
    parser.add_argument('--threads', type=int, default=4)
    parser.add_argument('--model-dir', type=Path)
    parser.add_argument('--run-id', help=argparse.SUPPRESS)
    parser.add_argument('--record-index', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--index-sha256', help=argparse.SUPPRESS)
    args = parser.parse_args()
    encode(args.input, args.output, args.device, args.batch_size, args.threads, args.model_dir, run_id=args.run_id,
           index_path=args.record_index, index_sha256=args.index_sha256)
