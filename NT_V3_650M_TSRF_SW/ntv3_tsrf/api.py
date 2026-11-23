"""Shared inference logic. User paths resolve against the caller's working directory."""
from contextlib import ExitStack
from functools import wraps
from os import SEEK_END
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace

from . import __version__
from .artifact_manifest import MANIFEST, SEAL, artifact_path, write_run_artifacts
from .checkpoints import DATABASE, RESUMABLE
from .common import ROOT, environment, offline_environment, read_json, sha256, verify_assets, verify_package, write_json
from .feature_metadata import matches_feature_contract, read_feature_metadata
from .model_lineage import prediction_binding
from .record_index import identity_digest, indexed_records
from .resource_plan import check_input_range, plan_resources, survey_input, temporary_directory
from .resources import model_location, INSTALLED, CODE_ROOT
from .run_record import (RunRecord, _record_root, _run_directory, computation_staging, load_run_record,
                         output_path, publish_output, reconcile_run,
                         record_path_value, require_new_output, reserve_output)
from .run_events import utc_now
from .output_metadata import (OUTPUT_FORMATS, WORKBOOK, _validate_context, _write_sidecars,
                              delivery_details, load_output_contract, parse_output_formats, write_output_metadata)
from .worker import run_process_tree


class IncFinderError(RuntimeError):
    """Input, environment, integrity or inference failure; never exits the host process."""

    def __init__(self, message, *, run_id=None, record_path=None, computed_output=None):
        super().__init__(message)
        self.run_id = run_id
        self.record_path = record_path
        self.computed_output = computed_output


def _prediction_parameters(*, input, output, device='cpu', batch_size=1, threads=4, jobs=1,
                           threshold_mode='ct', features_dir=None, model_dir=None, allow_encoder_drift=False,
                           max_length=10000, allow_out_of_domain=False, parent_run_id=None, resume_run_id=None,
                           output_formats=OUTPUT_FORMATS):
    """Return the run summary dictionary after offline per-record Inc typing.

    Input/output/features paths are relative to the caller's working directory.
    Output must not already exist. Resources are found within the software root.
    New encodings require allow_encoder_drift=True (research preview).
    Workers use this interpreter. The host CWD and environment are unchanged.
    parent_run_id optionally links a completed local run to this new prediction.
    resume_run_id verifies and reuses encoding batches from a stopped run in the same environment.
    output_formats='csv,npz' skips the optional XLSX export.
    Operational failures raise IncFinderError with run_id and record_path when available.
    If the requested XLSX fails, computed_output identifies the verified CSV/NPZ result.
    """
    return locals()


@wraps(_prediction_parameters, assigned=('__doc__',))
def predict(*args, **kwargs):
    return _predict(api_call=(args, kwargs))


def _parameter_snapshot(values, supplied):
    parameters = {name: {'value': None, 'source': 'explicit' if name in supplied else 'default'}
                  for name in values}
    for name in ('allow_encoder_drift', 'allow_out_of_domain'):
        parameters[name]['value'] = bool(values[name])
    for name in ('batch_size', 'threads', 'jobs', 'max_length'):
        value = values[name]
        if type(value) is int:
            parameters[name]['value'] = value
    for name, choices in (('device', ('cpu', 'cuda')), ('threshold_mode', ('ct', 'u05'))):
        parameters[name]['value'] = next((choice for choice in choices if values[name] == choice), None)
    return parameters


def _predict(*, path_base=None, embedding_python=None, rf_python=None, progress=None,
             entry_point='api', api_call=None, reproduction=None, **options):
    """Internal adapter shared with the legacy two-interpreter CLI."""
    try:
        run = RunRecord(entry_point)
    except (OSError, ValueError, RuntimeError) as exc:
        raise IncFinderError(f'Cannot create the local prediction record: {exc}') from exc

    with run:
        if reproduction is not None:
            run.data['reproduction'] = reproduction
        bound = False
        try:
            positional, supplied = api_call if api_call is not None else ((), options)
            values = _prediction_parameters(*positional, **supplied)
            bound = True
            run.data['parameters'] = _parameter_snapshot(values, supplied)
            args = SimpleNamespace(**values, path_base=path_base,
                                   embedding_python=embedding_python or sys.executable,
                                   rf_python=rf_python or sys.executable,
                                   single_environment=embedding_python is None and rf_python is None,
                                   progress=progress or (lambda message: None))
            for role, configured in (('embedding', embedding_python), ('report', embedding_python), ('rf', rf_python)):
                run.data['interpreters'][role] = {
                    'path': record_path_value(configured or sys.executable),
                    'source': 'explicit' if configured is not None else 'default',
                }
            summary, output, report_error = _run(args, run)
        except (Exception, KeyboardInterrupt) as exc:
            reference = run.reference
            published = run.has_verified_publication()
            try:
                if not published and not run.has_computation:
                    run.fail(exc)
            except (OSError, RuntimeError, ValueError) as record_error:
                reference += f'\nCould not finalize the local prediction record: {record_error}'
            if isinstance(exc, KeyboardInterrupt) or (not bound and api_call is not None and isinstance(exc, TypeError)):
                exc.run_id = run.run_id
                exc.record_path = run.path
                if isinstance(exc, KeyboardInterrupt):
                    exc.published = published
                exc.add_note(reference)
                raise
            message = str(exc)
            if published:
                message = f'Results were published. Run finalization was interrupted: {message}'
            elif run.has_computation:
                message += '; reconcile the sealed computation using this Run ID'
            elif 'diagnostic_directory' in run.data:
                message += f'; results were not published; diagnostic directory: {run.data["diagnostic_directory"]}'
            raise IncFinderError(f'{message}\n{reference}', run_id=run.run_id, record_path=run.path) from exc

        try:
            run.succeed()
        except (OSError, RuntimeError, ValueError) as exc:
            raise IncFinderError(f'Results were published, but the local prediction record could not be finalized: {exc}\n'
                                 f'{run.reference}', run_id=run.run_id, record_path=run.path) from exc
        if report_error is not None:
            message = f'Verified CSV/NPZ results are available at {output}. Use incfinder report to retry.'
            if isinstance(report_error, KeyboardInterrupt):
                report_error.run_id, report_error.record_path = run.run_id, run.path
                report_error.published, report_error.computed_output = True, output
                report_error.add_note(message + '\n' + run.reference)
                raise report_error
            raise IncFinderError(f'Requested XLSX report failed. {message}\n{run.reference}',
                                 run_id=run.run_id, record_path=run.path, computed_output=output) from report_error
        return summary


def worker(executable, module, arguments, log, device='cpu'):
    command = [executable, '-B', '-m', 'ntv3_tsrf.worker', module, *map(str, arguments)]
    # Keep the diagnostic tail available to the caller. Persist only the exit code
    # because child output can include paths, input records or credentials.
    with tempfile.TemporaryFile(mode='w+b') as stream:
        returncode = run_process_tree(command, cwd=CODE_ROOT, env=offline_environment(device), stdout=stream)
        log.write_text(f'{module} exit code {returncode}\n', encoding='utf-8')
        if returncode:
            end = stream.seek(0, SEEK_END)
            stream.seek(max(0, end - 3500))
            tail = stream.read().decode('utf-8', errors='replace')
            raise RuntimeError(f'{module} failed, exit code {returncode}. See {log.name}\n{tail}')


def _prepare_prediction(args, data, stage, run=None):
    parameters = data['parameters']
    stage('paths')
    output = output_path(args.output, args.path_base)
    parameters['output']['value'] = record_path_value(output)
    source = artifact_path(args.input, args.path_base)
    parameters['input']['value'] = record_path_value(source)
    require_new_output(output)
    if output.is_relative_to(ROOT) and not output.is_relative_to(ROOT / 'results'):
        raise ValueError('Outputs within the software root must be under results/')

    stage('arguments')
    if args.device not in ('cpu', 'cuda') or args.threshold_mode not in ('ct', 'u05'):
        raise ValueError('device must be cpu/cuda and threshold_mode must be ct/u05')
    for name in ('batch_size', 'threads', 'jobs', 'max_length'):
        value = getattr(args, name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f'{name} must be a positive integer')

    formats = parse_output_formats(args.output_formats)
    parameters['output_formats']['value'] = list(formats)

    stage('parent')
    resume = None
    if args.resume_run_id is None:
        if run is not None:
            run.link_parent(args.parent_run_id)
        elif args.parent_run_id is not None:
            from .run_events import TERMINAL

            if load_run_record(args.parent_run_id)['status'] not in TERMINAL:
                raise ValueError('parent_run_id must refer to a completed run')
            data['parent_run_id'] = args.parent_run_id
    else:
        if args.features_dir is not None:
            raise ValueError('resume_run_id cannot be combined with external precomputed features')
        if args.parent_run_id not in (None, args.resume_run_id):
            raise ValueError('parent_run_id must match resume_run_id when both are supplied')
        previous = (reconcile_run if run is not None else load_run_record)(args.resume_run_id)
        allowed = RESUMABLE if run is not None else RESUMABLE | {'running'}
        if previous['status'] not in allowed:
            raise ValueError('resume_run_id must refer to an interrupted, cancelled or failed encoding run')
        if not (_run_directory(args.resume_run_id) / 'checkpoints' / DATABASE).is_file():
            raise ValueError('Previous run has no trustworthy checkpoint database')
        resume = {key: previous[key] for key in ('run_id', 'attempt_id')}
        data['parent_run_id'] = parameters['resume_run_id']['value'] = previous['run_id']
        if args.parent_run_id is None:
            parameters['parent_run_id']['source'] = 'resume'
    parameters['parent_run_id']['value'] = data['parent_run_id']
    stage('output_contract')
    output_contract = load_output_contract()
    data['output_contract'] = output_contract
    stage('feature_paths')
    external_features = artifact_path(args.features_dir, args.path_base) if args.features_dir else None
    parameters['features_dir']['value'] = record_path_value(external_features) if external_features else None
    data['features'] = {'external': external_features is not None}
    stage('input')
    contract_path = ROOT / 'config/contract.json'
    contract = read_json(contract_path)
    limits = contract['validated_length_bp']
    survey = survey_input(source, limits, unique=run is None,
                          keep_outside=run is not None and args.allow_out_of_domain)
    facts = survey.facts
    data['input_sha256'] = facts['sha256']

    stage('environment')
    roles = 'rf' if external_features is not None else ('embedding', 'rf')
    launcher_environment = environment(roles) if run is not None and args.single_environment else None
    stage('model_location')
    selected_models = model_location(args.model_dir, args.path_base)
    model_dir = Path(selected_models['path'])
    parameters['model_dir']['value'] = record_path_value(model_dir)
    data['model'] = {'path': parameters['model_dir']['value'], 'source': selected_models.get('source')}
    if output.is_relative_to(model_dir):
        raise ValueError('Outputs must not be written into the model directory')
    if INSTALLED and any(output.is_relative_to(CODE_ROOT / name) for name in ('incfinder', 'ntv3_tsrf')):
        raise ValueError('Outputs must not be written into the installed package')
    stage('package')
    package_check = verify_package(model_dir) if run is not None else None
    data['package'] = package_check

    stage('scientific_contract')
    contract_hash = data['contract_sha256'] = sha256(contract_path)
    runtime_hash = data['runtime_policy_sha256'] = sha256(ROOT / 'config/runtime.json')

    stage('features')
    features_metadata_sha256 = None
    frozen_reference = False
    if external_features is not None:
        feature_metadata = artifact_path(external_features / 'features.json')
        feature_array = artifact_path(external_features / 'features.npy')
        if not feature_array.is_file():
            raise FileNotFoundError(f'Missing precomputed feature matrix: {feature_array}')
        features_metadata_sha256 = sha256(feature_metadata)
        feature_info, count, records_sha256 = read_feature_metadata(feature_metadata)
        if count != facts['record_count'] or records_sha256 != survey.records_sha256:
            raise ValueError('Precomputed features do not match input IDs, order or sequence hashes')
        if feature_info.get('shape') != [count, 1536] or feature_info.get('dtype') != 'float32':
            raise ValueError('Precomputed features must provide a float32 matrix with shape (record_count, 1536)')
        from .feature_metadata import verify_feature_header

        verify_feature_header(feature_array, count)
        if not matches_feature_contract(feature_info, feature_metadata, ROOT, contract_hash,
                                        sha256(ROOT / 'config/assets.json')):
            raise ValueError('Features are bound to a different encoder or model contract')
        reference_info, _, reference_sha = read_feature_metadata(ROOT / 'tests/fixtures/features.json')
        frozen_reference = (feature_info.get('scientific_equivalence') == 'FROZEN_REFERENCE'
                            and feature_info.get('features_sha256') == reference_info['features_sha256']
                            and records_sha256 == reference_sha)
    replay = data.get('reproduction')
    if replay:
        if facts['sha256'] != replay['input_sha256']:
            raise ValueError('Replayed input SHA-256 differs from the sealed source')
        if external_features is not None and (features_metadata_sha256 != replay['feature_metadata_sha256']
                                               or feature_info['features_sha256'] != replay['features_sha256']):
            raise ValueError('Replayed feature SHA-256 differs from the sealed source')
    preview = not frozen_reference
    stage('input_policy')
    data['research_preview'] = preview
    if preview and not args.allow_encoder_drift:
        raise ValueError('New encodings have not passed frozen embedding equivalence checks. For research preview, explicitly use --allow-encoder-drift; see docs/scientific_validation.md')
    data['out_of_domain_count'] = facts['outside_count']
    check_input_range(facts, limits, args.max_length, args.allow_out_of_domain)
    return SimpleNamespace(output=output, source=source, formats=formats, resume=resume,
                           output_contract=output_contract, external_features=external_features, survey=survey,
                           launcher_environment=launcher_environment, selected_models=selected_models,
                           model_dir=model_dir, package_check=package_check, contract_hash=contract_hash,
                           runtime_hash=runtime_hash, features_metadata_sha256=features_metadata_sha256,
                           preview=preview)


def _resources(args, prepared, checkpoint_directory):
    temporary = temporary_directory()
    verify_assets(quick=True, model_dir=prepared.model_dir)
    report = plan_resources(prepared.survey, output=prepared.output, run_dir=checkpoint_directory, temporary=temporary,
                            external_features=prepared.external_features, formats=prepared.formats,
                            asset_manifest=read_json(ROOT / 'config/assets.json'), device=args.device,
                            batch_size=args.batch_size, threads=args.threads, jobs=args.jobs)
    return temporary, report


def _dry_run(*, path_base=None, embedding_python=None, rf_python=None, **options):
    values = _prediction_parameters(**options)
    data = {'parameters': _parameter_snapshot(values, options), 'parent_run_id': None}
    args = SimpleNamespace(**values, path_base=path_base)
    prepared = _prepare_prediction(args, data, lambda stage: None)
    _, report = _resources(args, prepared, _record_root())
    return report


def _run(args, run):
    prepared = _prepare_prediction(args, run.data, run.enter_stage, run)
    output, source = prepared.output, prepared.source
    model_dir, formats = prepared.model_dir, prepared.formats
    external_features, output_contract = prepared.external_features, prepared.output_contract
    preview, survey = prepared.preview, prepared.survey
    input_facts = survey.facts
    input_hash = input_facts['sha256']
    parameters = run.data['parameters']
    contract_hash, runtime_hash = prepared.contract_hash, prepared.runtime_hash
    features_metadata_sha256 = prepared.features_metadata_sha256
    run.enter_stage('output_reservation')
    with reserve_output(output, run.run_id), ExitStack() as stack:
        run.enter_stage('assets')
        temporary_root, resources = _resources(args, prepared, run.path.parent)
        records = stack.enter_context(indexed_records(source, temp_dir=temporary_root))
        if len(records) != input_facts['record_count'] or identity_digest(records.identities()) != survey.records_sha256:
            raise ValueError('Input sequence identities changed after resource preflight')
        index_options = ('--record-index', records.path, '--index-sha256', records.sha256)
        args.progress('Verifying frozen model files (SHA-256)...')
        timings = {}
        tick = time.perf_counter()
        assets = verify_assets(model_dir=model_dir)
        timings['asset_verification_seconds'] = time.perf_counter() - tick
        run.data['model']['manifest_sha256'] = assets.get('manifest_sha256')
        lineage = prediction_binding(ROOT, installed=INSTALLED, run_id=run.run_id, input_sha256=input_hash)
        run.enter_stage('staging')
        temporary = computation_staging(output, run.run_id)
        temporary.mkdir(mode=0o700, exist_ok=False)
        try:
            (temporary / 'logs').mkdir()
            features = external_features or temporary / 'features'
            tick = time.perf_counter()
            if external_features is None:
                run.enter_stage('embedding')
                args.progress('Encoding forward and reverse-complement sequences in FP32...')
                run.prepare_checkpoints({
                    'input_sha256': input_hash, 'records_sha256': survey.records_sha256,
                    'model_manifest_sha256': assets['manifest_sha256'],
                    'contract_sha256': contract_hash, 'runtime_policy_sha256': runtime_hash,
                    'output_contract_sha256': sha256(ROOT / 'config/output_contract.json'),
                    'parameters': {key: parameters[key]['value'] for key in (
                        'device', 'batch_size', 'threads', 'jobs', 'threshold_mode', 'allow_encoder_drift',
                        'max_length', 'allow_out_of_domain',
                    )},
                }, prepared.resume)
                worker(args.embedding_python, 'ntv3_tsrf.embedding',
                       [source, features, '--device', args.device, '--batch-size', args.batch_size, '--threads', args.threads,
                        '--model-dir', model_dir, '--run-id', run.run_id, *index_options],
                       temporary / 'logs/embedding.log', args.device)
            timings['encoding_seconds'] = time.perf_counter() - tick
            run.enter_stage('classification')
            tick = time.perf_counter()
            args.progress('Running frozen two-stage random forests...')
            worker(args.rf_python, 'ntv3_tsrf.classify',
                   [source, features, temporary, '--threshold-mode', args.threshold_mode, '--jobs', args.jobs,
                    '--scientific-status', 'RESEARCH_PREVIEW' if preview else 'FROZEN_REFERENCE', '--model-dir', model_dir,
                    *index_options],
                   temporary / 'logs/rf.log')
            timings['rf_seconds'] = time.perf_counter() - tick
            run.enter_stage('prediction_contract')
            prediction = read_json(temporary / 'prediction.json')
            identity = _validate_context(prediction, output_contract['output_schema_version'],
                                         output_contract=output_contract)
            if sha256(source) != input_hash:
                raise ValueError('The input file changed during inference; results were not published')
            info, feature_rows, records_sha256 = read_feature_metadata(features / 'features.json')
            if (feature_rows != len(records) or records_sha256 != survey.records_sha256
                    or sha256(records.path) != records.sha256):
                raise ValueError('Input sequence identities changed; results were not published')
            write_output_metadata(temporary, output_contract=output_contract, formats=('csv', 'npz'), lineage=lineage)
            if lineage is not None:
                identity['model_lineage'] = lineage
            run.data['interpreters']['report'].update(invoked=False, python_version=None)
            for role, versions in (('embedding', None if external_features else info['environment']),
                                   ('rf', prediction.get('environment'))):
                interpreter = run.data['interpreters'][role]
                interpreter['invoked'] = role != 'embedding' or external_features is None
                interpreter['python_version'] = versions.get('python') if versions else None
            run.data['features']['sha256'] = info['features_sha256']
            summary = {
                'software': 'NT_V3_650M_TSRF_SW', 'version': __version__, 'run_id': run.run_id,
                'status': 'RESEARCH_PREVIEW' if preview else 'PASS',
                'execution_status': 'PASS',
                'scientific_equivalence': 'NOT_ESTABLISHED' if preview else 'FROZEN_REFERENCE',
                'started_utc': run.data['started_utc'], 'completed_utc': utc_now(),
                'input': {'name': source.name, 'sha256': input_hash, 'record_count': len(records),
                          'format': input_facts['format'], 'length_min': input_facts['length_min'],
                          'length_max': input_facts['length_max']},
                'input_unit': 'one_sequence_record', 'fastq_quality_used_for_inference': False,
                'out_of_domain_rows': survey.outside_rows, 'assets': assets,
                'model_location': prepared.selected_models, 'timings': timings,
                'package': prepared.package_check, 'resource_plan': resources,
                'contract_sha256': contract_hash,
                'runtime_policy_sha256': runtime_hash,
                **output_contract,
                'launcher_environment': prepared.launcher_environment,
                'report_environment': None,
                'features_sha256': info['features_sha256'], 'embedding_environment': info['environment'],
                'embedding_device': info['device'], 'prediction': prediction,
                'outputs': {p.name: sha256(p) for p in temporary.iterdir() if p.is_file()},
            }
            if 'reproduction' in run.data:
                summary['reproduction'] = run.data['reproduction']
            if lineage is not None:
                summary['model_lineage'] = lineage
            # Seal the verified computation before starting the report process.
            # Its immutable envelope also describes the result after a hard interruption.
            report_status = 'interrupted' if 'xlsx' in formats else 'not_requested'
            summary['delivery'] = delivery_details(formats, report_status, summary['outputs'])
            write_json(temporary / 'run_summary.json', summary)
            verify_assets(quick=True, model_dir=model_dir)
            manifest_sha256 = write_run_artifacts(temporary, summary, source, external_features, features_metadata_sha256)
            run.seal_computation(temporary, manifest_sha256)
            report_error = None
            if 'xlsx' in formats:
                report_status, report_error = _add_report(args, run, temporary, identity, summary)
                summary['delivery'] = delivery_details(formats, report_status, summary['outputs'])
                summary['completed_utc'] = utc_now()
                (temporary / MANIFEST).unlink()
                (temporary / SEAL).unlink()
                write_json(temporary / 'run_summary.json', summary)
                manifest_sha256 = write_run_artifacts(temporary, summary, source, external_features,
                                                     features_metadata_sha256)

            run.enter_stage('result_validation')
            run.prepare_publication(temporary / 'run_summary.json', artifact_manifest_sha256=manifest_sha256,
                                    report_status=report_status)
            publish_output(temporary, output)
        except (Exception, KeyboardInterrupt) as exc:
            if not run.has_computation:
                run.data['diagnostic_directory'] = record_path_value(temporary)
                try:
                    write_json(temporary / 'failure.json',
                               {'status': 'CANCELLED' if isinstance(exc, KeyboardInterrupt) else 'FAILED',
                                'run_id': run.run_id, 'error': run.failure_details(exc)})
                except OSError:
                    pass  # Keep the original failure available through the central run record.
            raise
    return summary, output, report_error


def _add_report(args, run, core, identity, summary):
    run.data['interpreters']['report']['invoked'] = True
    run.enter_stage('report')
    tick = time.perf_counter()
    args.progress('Writing and reading back the XLSX workbook...')
    with tempfile.TemporaryDirectory(prefix='.ntv3-report-', dir=core.parent) as workspace:
        report = Path(workspace)
        try:
            worker(args.embedding_python, 'ntv3_tsrf.report', [core, '--output', report], report / 'report.log')
            _write_sidecars(report, identity, (WORKBOOK,))
            versions = read_json(report / 'report_environment.json')
            if not isinstance(versions, dict):
                raise ValueError('Invalid report environment receipt')
        except (Exception, KeyboardInterrupt) as exc:
            summary['timings']['report_seconds'] = time.perf_counter() - tick
            return ('interrupted' if isinstance(exc, KeyboardInterrupt) else 'failed'), exc
        for name in (WORKBOOK, WORKBOOK + '.metadata.json', 'report_environment.json'):
            (report / name).rename(core / name)
            summary['outputs'][name] = sha256(core / name)
        if (report / 'report.log').is_file():
            (report / 'report.log').rename(core / 'logs/report.log')
    summary['timings']['report_seconds'] = time.perf_counter() - tick
    summary['report_environment'] = versions
    run.data['interpreters']['report']['python_version'] = versions.get('python')
    return 'succeeded', None
