"""Portable XLSX exporter used by the software, independent of desktop tools."""
import argparse
import csv
from tempfile import TemporaryDirectory
from pathlib import Path
from .common import environment, read_json, sha256, write_json
from .output_metadata import CSV_RESULT_FILES, WORKBOOK, _write_sidecars, verify_result_metadata


def export_workbook(source, destination=None):
    """Render saved CSVs to XLSX, optionally in a distinct staging directory."""
    source = Path(source)
    destination = source if destination is None else Path(destination)
    versions = environment('report')
    from openpyxl import Workbook, load_workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    prediction = read_json(source / 'prediction.json')
    tables = prediction['tables']
    if not isinstance(tables, dict) or set(tables) != set(CSV_RESULT_FILES):
        raise ValueError('Prediction must declare the four supported CSV tables')
    sheet_names = ['Stage1', 'Stage2_probability', 'Inc_prediction', 'Inc_result']
    workbook = Workbook(write_only=True)
    numeric = {'row_index', 'Length_bp', 'Stage1_Probability', 'Stage1_threshold', 'gate_pass',
               'Inc_unclassified', 'Inc_classified', 'predicted_label_count'}
    labels = set(prediction['label_order'])
    sheet_counts = {}
    sheet_fields = {}
    for filename, title in zip(CSV_RESULT_FILES, sheet_names):
        fields = tables[filename]
        def new_sheet(part):
            sheet = workbook.create_sheet(title if part == 1 else f'{title}_{part}')
            sheet.freeze_panes = 'C2'
            for index, name in enumerate(fields, 1):
                sheet.column_dimensions[get_column_letter(index)].width = 42 if name in {'Header', 'Result', 'predicted_labels'} else max(14, min(28, len(name) + 2))
            cells = []
            for name in fields:
                cell = WriteOnlyCell(sheet, name)
                cell.font = Font(name='Calibri', bold=True, color='FFFFFF')
                cell.fill = PatternFill('solid', fgColor='244A64')
                cell.alignment = Alignment(wrap_text=True, vertical='center')
                cells.append(cell)
            sheet.append(cells)
            sheet.row_dimensions[1].height = 32
            sheet_fields[sheet.title] = tuple(fields)
            return sheet
        part, rows = 1, 0
        sheet = new_sheet(part)
        with (source / filename).open(encoding='utf-8-sig', newline='') as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != fields:
                raise ValueError(f'Unexpected report input columns: {filename}')
            for row in reader:
                if rows == 1048575:
                    sheet.auto_filter.ref = f'A1:{get_column_letter(len(fields))}{rows + 1}'
                    sheet_counts[sheet.title] = rows
                    part, rows = part + 1, 0
                    sheet = new_sheet(part)
                cells = []
                for name in fields:
                    value = row[name]
                    is_number = name in numeric or name in labels or name.startswith('probability__')
                    if is_number and value != 'NA':
                        value = float(value) if '.' in value or 'e' in value.lower() else int(value)
                    cell = WriteOnlyCell(sheet, value)
                    if isinstance(value, str):
                        cell.data_type = 's'
                    elif name.startswith('probability__') or name == 'Stage1_Probability':
                        cell.number_format = '0.000000'
                    cells.append(cell)
                sheet.append(cells)
                rows += 1
        sheet.auto_filter.ref = f'A1:{get_column_letter(len(fields))}{rows + 1}'
        sheet_counts[sheet.title] = rows
    path = destination / WORKBOOK
    workbook.save(path)
    check = load_workbook(path, read_only=True, data_only=False)
    try:
        if check.sheetnames != list(sheet_counts):
            raise ValueError('XLSX read-back sheet order mismatch')
        for sheet in check:
            rows = sheet.values
            if next(rows, None) != sheet_fields[sheet.title] or sum(1 for _ in rows) != sheet_counts[sheet.title]:
                raise ValueError(f'XLSX read-back row count or columns mismatch: {sheet.title}')
    finally:
        check.close()
    write_json(destination / 'report_environment.json', versions)


def rebuild_report(directory, destination, *, expected_manifest_sha256=None):
    """Publish a derived XLSX report from sealed prediction artifacts, without model workers."""
    from .artifact_lifecycle import inspect_artifacts
    from .artifact_manifest import MANIFEST, SEAL, _facts, artifact_path
    from .resources import CODE_ROOT, INSTALLED, ROOT
    from .run_events import utc_now
    from .run_record import output_path, publish_output, reserve_output

    source, target = artifact_path(directory), output_path(destination, None)
    if target.is_relative_to(source):
        raise ValueError('Report destination must be outside the source result')
    if target.is_relative_to(ROOT) and not target.is_relative_to(ROOT / 'results'):
        raise ValueError('Reports within the software root must be under results/')
    if INSTALLED and any(target.is_relative_to(CODE_ROOT / name) for name in ('incfinder', 'ntv3_tsrf')):
        raise ValueError('Reports must not be written into the installed package')

    view = inspect_artifacts(source, expected_manifest_sha256=expected_manifest_sha256, read_references=False)
    if view.summary is None or any(reason != 'retention' for reason in view.missing.values()):
        raise ValueError('Missing artifact required by the sealed report source')
    manifest, digest, summary = view.manifest, view.digest, view.summary
    journal = view.publication
    prediction = read_json(source / 'prediction.json')
    identity = verify_result_metadata(source, summary, prediction)
    names = ('prediction.json', *CSV_RESULT_FILES)
    source_entries = {item['path']: item for item in manifest['artifacts'] if item['storage'] == 'retained'}
    seal_digest = sha256(source / SEAL)
    with reserve_output(target, manifest['run_id']), TemporaryDirectory(prefix='.incfinder-report-', dir=target.parent) as tmp:
        staged = Path(tmp) / 'report'
        staged.mkdir()
        export_workbook(source, staged)
        _write_sidecars(staged, identity, (WORKBOOK,))
        if sha256(source / MANIFEST) != digest or sha256(source / SEAL) != seal_digest:
            raise ValueError('Source artifact seal changed during report generation')
        for name in names:
            entry = source_entries[name]
            if _facts(source / name, name, stored=True) != (entry['size_bytes'], entry['sha256']):
                raise ValueError(f'Source artifact changed during report generation: {name}')
        files = (WORKBOOK, WORKBOOK + '.metadata.json', 'report_environment.json')
        receipt = {
            'schema_version': 1, 'kind': 'derived_xlsx_report', 'completed_utc': utc_now(),
            'source': {'run_id': manifest['run_id'], 'kind': manifest['kind'], 'manifest_sha256': digest,
                       'publication_event_bound': journal is not None,
                       **({'run_manifest_sha256': manifest['source_manifest_sha256']}
                          if manifest['kind'] == 'archive' else {})},
            'inputs': {name: source_entries[name]['sha256'] for name in names},
            'outputs': {name: sha256(staged / name) for name in files},
            'relations': [{'type': 'wasDerivedFrom', 'generated_entity': WORKBOOK,
                           'used_entity': source_entries[name]['artifact_id']} for name in names],
        }
        write_json(staged / 'report_receipt.json', receipt)
        publish_output(staged, target)
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    export_workbook(args.source, args.output)
