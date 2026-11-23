"""File-based IncFinder CLI, with legacy argument and path compatibility."""
import argparse
import json
import sys
from . import __version__
from .api import IncFinderError, _dry_run, _predict
from .common import ROOT, doctor, read_json, verify_assets, verify_package
from .run_record import RunRecord


def prediction_arguments(parser, legacy=False):
    parser.add_argument('input_file', nargs='?', help='FASTA/FASTQ file, optionally gzip-compressed')
    parser.add_argument('--input', '--i', '-i', help='Input file; choose either this option or the positional input')
    parser.add_argument('--output', '--o', '-o', required=True, help='New output directory that does not already exist')
    parser.add_argument('--model-dir', help='Model directory containing ntv3/ and rf/')
    if legacy:
        parser.add_argument('--embedding-python', help='Legacy interface: interpreter for encoding and XLSX export')
        parser.add_argument('--rf-python', help='Legacy interface: interpreter for RF inference')
    parser.add_argument('--device', choices=['cpu', 'cuda'])
    parser.add_argument('--batch-size', type=int)
    parser.add_argument('--threads', type=int)
    parser.add_argument('--jobs', type=int)
    parser.add_argument('--threshold-mode', choices=['ct', 'u05'])
    parser.add_argument('--dry-run', action='store_true', help='Survey resources without creating a run or outputs')
    parser.add_argument('--output-formats', help='csv,npz for calculation only; default csv,npz,xlsx')
    parser.add_argument('--features-dir', help='Directory containing features.npy and identity-bound features.json')
    parser.add_argument('--allow-encoder-drift', action='store_true', help='Accept unestablished encoder equivalence for research preview')
    parser.add_argument('--max-length', type=int)
    parser.add_argument('--allow-out-of-domain', action='store_true')
    parser.add_argument('--parent-run-id', help='Link this prediction to a completed local run')
    parser.add_argument('--resume-run-id', help='Reuse verified encoding batches from a stopped local run')


def main(argv=None, *, legacy=False):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog='run.py' if legacy else 'incfinder',
        description='NT-v3 650M + two-stage RF Inc typing; CPU by default; offline inference.',
        epilog=('Legacy relative paths resolve from the software root.' if legacy else
                'Prediction shorthand: incfinder --input INPUT --output OUTPUT; relative paths resolve from the current directory.'))
    parser.add_argument('--version', action='version', version=f'incfinder {__version__}')
    subs = parser.add_subparsers(dest='command', title='Commands')
    commands = {}
    commands['predict'] = subs.add_parser('predict', argument_default=argparse.SUPPRESS,
                                           help='Predict Inc labels from FASTA/FASTQ/gzip files')
    prediction_arguments(commands['predict'], legacy)
    commands['doctor'] = subs.add_parser('doctor', help='Check dependencies and model locations without loading models or probing GPUs')
    commands['doctor'].add_argument('--model-dir')
    commands['check'] = subs.add_parser('check', help='Verify software resources and model SHA-256 hashes')
    commands['check'].add_argument('--model-dir')
    commands['check'].add_argument('--quick', action='store_true', help='Check model sizes only; this is not full SHA-256 verification')
    models = commands['models'] = subs.add_parser('models', help='Verify or install the frozen model files')
    model_actions = models.add_subparsers(dest='model_action', required=True)
    model_verify = model_actions.add_parser('verify', help='Check the complete model SHA-256 hashes offline')
    model_verify.add_argument('--model-dir', help='Existing model directory with ntv3/ and rf/')
    model_verify.add_argument('--parts-dir', help='Optional directory of frozen RF volumes to verify')
    model_install = model_actions.add_parser('install', help='Stage, verify and publish missing model files')
    model_install.add_argument('--model-dir', required=True, help='Destination model directory')
    model_install.add_argument('--parts-dir', help='Read-only directory of local RF volumes')
    model_install.add_argument('--ntv3-dir', help='Read-only directory of local NTv3 files')
    model_install.add_argument('--cache-dir', help='Parent directory for the resumable download cache')
    model_install.add_argument('--download-rf', action='store_true', help='Explicitly allow downloading missing frozen RF volumes')
    model_install.add_argument('--download-ntv3', action='store_true', help='Explicitly allow downloading missing gated NTv3 files')
    lineage = commands['lineage'] = subs.add_parser('lineage', help='Inspect verified prediction lineage or export offline pages')
    lineage.add_argument('directory', help='Completed prediction or verified archive directory')
    choice = lineage.add_mutually_exclusive_group()
    choice.add_argument('--row', type=int, help='Inspect one zero-based result row')
    choice.add_argument('--output', help='New directory for the JSON and offline HTML export')
    lineage.add_argument('--start', type=int, help='First row of the export')
    lineage.add_argument('--stop', type=int, help='Exclusive end of the export range')
    lineage.add_argument('--page-size', type=int, help='Maximum records per page, from 1 to 100')
    lineage.add_argument('--manifest-sha256', help='Expected digest from a trusted manifest receipt')
    lineage.add_argument('--reference-map', help='JSON mapping of redacted artifact IDs to local paths')
    report = commands['report'] = subs.add_parser('report', help='Rebuild an XLSX report from verified calculation results')
    report.add_argument('directory', help='Completed prediction or sealed archive directory')
    report.add_argument('--output', required=True, help='New derived report directory')
    report.add_argument('--manifest-sha256', help='Expected digest from a trusted manifest receipt')
    share = commands['export-share'] = subs.add_parser('export-share', help='Export an allowlisted view of sealed artifacts')
    share.add_argument('directory', help='Completed prediction or sealed archive directory')
    share.add_argument('--output', required=True, help='New directory for share_manifest.json and lineage.json')
    share.add_argument('--replay-from', choices=('sequence', 'features', 'report'), default='features')
    share.add_argument('--manifest-sha256', help='Expected digest from a trusted manifest receipt')
    share.add_argument('--reference-map', help='JSON mapping of original artifact IDs to explicit local paths')
    share.add_argument('--model-dir', help='Explicit local model binding for replay assessment')
    share.add_argument('--result-field', action='append', default=[],
                       choices=('gate_pass', 'predicted_label_count', 'internal_status'),
                       help='Approve one typed column of inc_results.csv for this share')
    share.add_argument('--html', action='store_true', help='Include an offline file-lineage page')
    retention = commands['retention'] = subs.add_parser('retention', help='Preview or apply a local Run retention policy')
    retain = retention.add_subparsers(dest='retention_action', required=True)
    for action in ('plan', 'apply'):
        item = retain.add_parser(action)
        item.add_argument('directory', help='Original locally published Run directory')
        item.add_argument('--policy', choices=('minimal', 'standard', 'full'), required=True)
        if action == 'apply':
            item.add_argument('--plan-sha256', required=True, help='Digest returned by retention plan')
    replay = commands['reproduce'] = subs.add_parser('reproduce', help='Replay a verified Run or archive into a new Run')
    replay.add_argument('source', help='Local Run ID or sealed archive directory')
    replay.add_argument('--output', required=True, help='New prediction output directory')
    replay.add_argument('--model-dir', help='Explicit model directory, required when replaying an archive')
    replay.add_argument('--reference-map', help='JSON mapping of external artifact IDs to local paths')
    replay.add_argument('--manifest-sha256', help='Trusted source manifest digest from a separate receipt')
    replay.add_argument('--dry-run', action='store_true', help='Verify dependencies and show the replay parameters')
    compare = commands['compare'] = subs.add_parser('compare', help='Compare two verified Runs or archives')
    compare.add_argument('baseline')
    compare.add_argument('candidate')
    compare.add_argument('--output', required=True, help='New directory for comparison.json and comparison.md')
    compare.add_argument('--encoder-profile', choices=('ntv3_650m_cpu_fp32_v1',),
                         help='Apply the declared CPU FP32 embedding tolerance when the recorded settings match')
    for side in ('baseline', 'candidate'):
        compare.add_argument(f'--{side}-reference-map', help='JSON mapping of external artifact IDs to local paths')
        compare.add_argument(f'--{side}-manifest-sha256', help='Trusted source manifest digest from a separate receipt')
    archive = commands['archive'] = subs.add_parser('archive', help='Create or verify a private RO-Crate research archive')
    actions = archive.add_subparsers(dest='archive_action', required=True)
    for action in ('create', 'verify'):
        item = actions.add_parser(action)
        item.add_argument('directory', help='Completed Run result for create, or research archive for verify')
        item.add_argument('--reference-map', help='JSON mapping of external artifact IDs to local paths')
        item.add_argument('--manifest-sha256', help='Trusted source manifest digest from a separate receipt')
        if action == 'create':
            item.add_argument('--output', required=True, help='New private archive directory')
            item.add_argument('--external-mode', choices=('reference', 'copy'), default='reference')
        else:
            item.add_argument('--model-dir', help='Verify the referenced frozen models against a local directory')
    commands['version'] = subs.add_parser('version', help='Show the software version')
    commands['help'] = subs.add_parser('help', help='Show general help or help for one command')
    commands['help'].add_argument('topic', nargs='?', choices=list(commands))
    if not argv:
        parser.print_help()
        return 0
    if not legacy and argv[0].startswith('-') and argv[0] not in ('--help', '-h', '--version'):
        argv.insert(0, 'predict')
    entry_point = 'legacy_cli' if legacy else 'cli'
    try:
        args = parser.parse_args(argv)
        if args.command == 'predict':
            options = vars(args)
            named, positional = options.get('input'), options.pop('input_file', None)
            if bool(named) == bool(positional):
                commands['predict'].error('Provide exactly one positional input or --input/-i; stdin is not supported')
            args.input = named or positional
            if args.input == '-':
                commands['predict'].error('This release requires file input; stdin is not supported')
    except SystemExit as exc:
        if argv[0] == 'predict' and exc.code == 2 and '--dry-run' not in argv:
            try:
                with RunRecord(entry_point) as run:
                    run.fail(exc)
                    print(run.reference, file=sys.stderr)
            except (OSError, RuntimeError, ValueError) as record_error:
                print(f'ERROR: Cannot write the local prediction record: {record_error}', file=sys.stderr)
        raise
    if args.command == 'help':
        (commands[args.topic] if args.topic else parser).print_help()
        return 0
    if args.command == 'version':
        print(f'incfinder {__version__}')
        return 0
    try:
        if args.command == 'doctor':
            result = doctor(args.model_dir)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result['status'] == 'PASS' else 2
        if args.command == 'check':
            print(json.dumps({'package': verify_package(args.model_dir),
                              'assets': verify_assets(args.quick, args.model_dir),
                              'deployment': read_json(ROOT / 'config/deployment_status.json')}, ensure_ascii=False, indent=2))
            return 0
        if args.command == 'models':
            from .model_install import install_models, verify_models

            base = ROOT if legacy else None
            if args.model_action == 'verify':
                result = verify_models(args.model_dir, args.parts_dir, base=base)
            else:
                result = install_models(args.model_dir, args.parts_dir, args.ntv3_dir, args.cache_dir,
                                        download_rf=args.download_rf, download_ntv3=args.download_ntv3, base=base,
                                        progress=lambda message: print(message, file=sys.stderr, flush=True))
            print(json.dumps(result, ensure_ascii=True, indent=2))
            return 0
        if args.command == 'report':
            from .artifact_manifest import artifact_path
            from .report import rebuild_report
            from .run_record import output_path
            base = ROOT if legacy else None
            result = rebuild_report(artifact_path(args.directory, base), output_path(args.output, base),
                                    expected_manifest_sha256=args.manifest_sha256)
            print(json.dumps(result, ensure_ascii=True, indent=2, allow_nan=False))
            return 0
        if args.command in ('export-share', 'retention'):
            result = _share_or_retain(args, ROOT if legacy else None)
            print(json.dumps(result, ensure_ascii=True, indent=2, allow_nan=False))
            return 0
        if args.command == 'lineage':
            result = _lineage(args, ROOT if legacy else None)
            print(json.dumps(result, ensure_ascii=True, indent=2, allow_nan=False))
            return 0
        if args.command in ('reproduce', 'compare', 'archive'):
            result = _research_workflow(args, ROOT if legacy else None)
            print(json.dumps(result, ensure_ascii=True, indent=2, allow_nan=False))
            return 1 if args.command == 'compare' and result['status'] not in ('identical', 'within_tolerance') else 0
        options = vars(args).copy()
        options.pop('command')
        base = ROOT if legacy else None
        if options.pop('dry_run', False):
            summary = _dry_run(**options, path_base=base)
        else:
            summary = _predict(**options, path_base=base, entry_point=entry_point,
                               progress=lambda text: print(text, file=sys.stderr, flush=True))
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt as exc:
        run_id = getattr(exc, 'run_id', None)
        message = 'Interrupted after publication.' if getattr(exc, 'published', False) else 'Cancelled.'
        if getattr(exc, 'computed_output', None) is not None:
            message += f' Verified CSV/NPZ results are available at {exc.computed_output}.'
        if run_id is not None:
            message += f' Run ID {run_id} (record: {exc.record_path})'
        print(message, file=sys.stderr)
        return 130
    except (IncFinderError, ValueError, OSError, RuntimeError, KeyError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2


def _lineage(args, base):
    from .artifact_manifest import artifact_path
    from .lineage import inspect_lineage
    from .lineage_export import export_lineage

    source = artifact_path(args.directory, base)
    reference_paths = _reference_map(args.reference_map, base)
    options = {'expected_manifest_sha256': args.manifest_sha256, 'reference_paths': reference_paths}
    paging = {name: getattr(args, name) for name in ('start', 'stop', 'page_size') if getattr(args, name) is not None}
    if args.output is not None:
        return export_lineage(source, artifact_path(args.output, base), **options, **paging)
    if paging:
        raise ValueError('Row range and page size require --output')
    return inspect_lineage(source, row_index=args.row, **options)


def _reference_map(filename, base):
    from .artifact_manifest import artifact_path

    if filename is None:
        return None
    bindings = read_json(artifact_path(filename, base))
    if not isinstance(bindings, dict) or any(not isinstance(path, str) or not path for path in bindings.values()):
        raise ValueError('Reference map must map artifact IDs to path strings')
    return {key: artifact_path(value, base) for key, value in bindings.items()}


def _share_or_retain(args, base):
    from .artifact_manifest import artifact_path

    source = artifact_path(args.directory, base)
    if args.command == 'export-share':
        from .sharing import export_share

        models = artifact_path(args.model_dir, base) if args.model_dir is not None else None
        return export_share(source, artifact_path(args.output, base), replay_from=args.replay_from,
                            expected_manifest_sha256=args.manifest_sha256,
                            reference_paths=_reference_map(args.reference_map, base), model_dir=models,
                            result_fields=args.result_field, html=args.html)

    from .retention import apply_retention, plan_retention

    if args.retention_action == 'plan':
        return plan_retention(source, args.policy)
    return apply_retention(source, args.policy, expected_plan_sha256=args.plan_sha256)


def _research_workflow(args, base):
    from .artifact_manifest import artifact_path
    from .comparison import compare_results
    from .reproduction import reproduce
    from .research_archive import export_research_archive, verify_research_archive
    from .run_events import canonical_uuid

    def resolve(value):
        return artifact_path(value, base) if value is not None else None

    if args.command == 'compare':
        result = compare_results(resolve(args.baseline), resolve(args.candidate), destination=resolve(args.output),
                                 baseline_reference_paths=_reference_map(args.baseline_reference_map, base),
                                 candidate_reference_paths=_reference_map(args.candidate_reference_map, base),
                                 baseline_manifest_sha256=args.baseline_manifest_sha256,
                                 candidate_manifest_sha256=args.candidate_manifest_sha256,
                                 encoder_profile=args.encoder_profile)
        return {'status': result['status'], 'output': str(resolve(args.output)),
                'incomparable_reasons': result['incomparable_reasons']}
    options = {'expected_manifest_sha256': args.manifest_sha256,
               'reference_paths': _reference_map(args.reference_map, base)}
    if args.command == 'reproduce':
        source = args.source if canonical_uuid(args.source) else resolve(args.source)
        return reproduce(source, resolve(args.output), **options, model_dir=resolve(args.model_dir), dry_run=args.dry_run,
                         progress=lambda text: print(text, file=sys.stderr, flush=True))
    if args.archive_action == 'create':
        return export_research_archive(resolve(args.directory), resolve(args.output), **options,
                                       external_mode=args.external_mode)
    return verify_research_archive(resolve(args.directory), **options, model_dir=resolve(args.model_dir))


def legacy_main():
    return main(legacy=True)
