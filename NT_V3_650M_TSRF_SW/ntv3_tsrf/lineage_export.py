"""Export a portable batch overview and bounded record pages."""
from html import escape
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from .artifact_manifest import artifact_path
from .lineage import file_graph, record_graph
from .lineage_evidence import load_evidence
from .resources import CODE_ROOT, INSTALLED, ROOT
from .run_record import publish_output, require_new_output, reserve_output


_STATES = {'Unclassified': 'Gate rejected', 'Inc_no_call': 'Gate passed, no label called', 'Inc_called': 'Label called'}
_STAGE_ORDER = {'embedding': 0, 'classification': 1, 'report': 2, 'publication': 3}
_STYLE = """
:root{color-scheme:light;--ink:#142134;--muted:#526277;--edge:#dce4ec;--accent:#066d71;--wash:#edf7f6;--paper:#fff}
*{box-sizing:border-box}body{margin:0;background:#f5f8fa;color:var(--ink);font:15px/1.52 system-ui,sans-serif}
main{max-width:1140px;margin:auto;padding:2.75rem clamp(1rem,4vw,3rem) 4rem}h1,h2,h3{line-height:1.2}
h1{margin:0 0 .55rem;font-size:clamp(1.8rem,4vw,2.8rem)}h2{margin-top:2.1rem;font-size:1.25rem}h3{margin:.2rem 0}
a{color:#075f63}a:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
code{overflow-wrap:anywhere;color:#23465c}.meta{color:var(--muted);margin:.25rem 0 .75rem}
nav{display:flex;flex-wrap:wrap;gap:.65rem 1.15rem;margin:1.25rem 0}
.metrics,.pipeline{display:flex;flex-wrap:wrap;gap:.7rem;padding:0;list-style:none}
.metrics>li,.pipeline>li{flex:1 1 155px;padding:.75rem 1rem;background:var(--paper);border:1px solid var(--edge);border-radius:.6rem}
.metrics strong{display:block;color:var(--accent);font-size:1.65rem}.pipeline>li{border-top:3px solid var(--accent)}
.pipeline small{display:block;margin-top:.2rem;color:var(--muted)}
.table{overflow:auto;border:1px solid var(--edge);border-radius:.55rem;background:var(--paper)}
table{border-collapse:collapse;width:100%;text-align:left}th,td{padding:.55rem .85rem;border-bottom:1px solid var(--edge);vertical-align:top}
th{background:var(--wash);font-weight:650}tr:last-child td{border-bottom:0}
details{border:1px solid var(--edge);background:var(--paper);border-radius:.55rem;padding:.6rem .9rem;margin:.65rem 0}
summary{cursor:pointer;font-weight:650}details h3{margin-top:1.1rem}.payload{overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere}
.record{padding:1rem 1.15rem;border:1px solid var(--edge);border-radius:.65rem;background:var(--paper);margin:.85rem 0}
.state{color:#124b50;background:var(--wash);padding:.13rem .45rem;border-radius:.3rem;font-size:.9rem}
"""


def _text(value):
    return escape(str(value), quote=True)


def _html(title, body):
    title = _text(title)
    return ('<!doctype html>\n<html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" '
            'content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">'
            f'<title>{title}</title><style>{_STYLE}</style><main>{body}</main></html>\n')


def _table(heads, rows):
    header = ''.join('<th scope="col">' + _text(cell) + '</th>' for cell in heads)
    body = ''.join('<tr>' + ''.join('<td>' + _text(cell) + '</td>' for cell in row) + '</tr>' for row in rows)
    return f'<div class="table"><table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table></div>'


def _pipeline(graph, batch=False):
    activities = graph['activities']
    if batch:
        activities = sorted(activities, key=lambda node: _STAGE_ORDER[node['stage']])
    content = []
    for node in activities:
        activity = node['id']
        inputs = sum(r['type'] == 'used' and r['activity'] == activity for r in graph['relations'])
        outputs = sum(r['type'] == 'wasGeneratedBy' and r['activity'] == activity for r in graph['relations'])
        content.append(f'<li><strong>{_text(node["label"])}</strong><small>{inputs} inputs · {outputs} outputs</small></li>')
    return '<ol class="pipeline">' + ''.join(content) + '</ol>'


def _relationships(graph):
    labels = {item['id']: item['label'] for group in ('entities', 'activities') for item in graph[group]}
    rows = []
    for relation in graph['relations']:
        kind = relation['type']
        if kind == 'wasDerivedFrom':
            left, right = relation['generated_entity'], relation['used_entity']
        elif kind == 'used':
            left, right = relation['activity'], relation['entity']
        else:
            left, right = relation['entity'], relation['activity']
        rows.append((labels[left], kind, labels[right]))
    return _table(('Subject', 'Relation', 'Object'), rows)


def _overview_html(graph):
    ident, selection = graph['identity'], graph['selection']
    counts = graph['status_counts']
    items = [('Records', graph['record_count']), *((label, counts[key]) for key, label in _STATES.items())]
    stats = ''.join(f'<li><strong>{number}</strong>{_text(label)}</li>' for label, number in items)
    entities = [e for e in graph['entities'] if e['kind'] == 'file']
    has_availability = any('availability' in e for e in entities)
    columns = ('Artifact', 'Storage', 'Bytes', 'SHA-256') + (('Availability',) if has_availability else ())
    files = [(e['label'], e['storage'], e['size_bytes'], e['sha256']) +
             ((e.get('availability', 'available'),) if has_availability else ()) for e in entities]
    start, stop = selection['start'], selection['stop']
    body = (
        '<h1>IncFinder lineage</h1>'
        f'<p class="meta">Run <code>{_text(ident["run_id"])}</code> · {_text(ident["threshold_mode"])} · '
        f'{_text(ident["scientific_status"])}</p>'
        f'<p class="meta">Manifest SHA-256 <code>{_text(ident["manifest_sha256"])}</code></p>'
        f'<nav aria-label="Export navigation"><a href="rows-{start}.html">Browse rows {start}–{stop - 1}</a>'
        '<a href="lineage.json">Batch JSON</a></nav>'
        f'<ul class="metrics" aria-label="Batch totals">{stats}</ul>'
        f'<h2>Batch activities</h2>{_pipeline(graph, batch=True)}'
        f'<h2>Files</h2>{_table(columns, files)}'
        f'<details><summary>File relationships</summary>{_relationships(graph)}</details>'
    )
    return _html('IncFinder lineage', body)


def _record_html(graph):
    index, row = graph['row_index'], graph['row']
    labels = ', '.join(row['called_labels']) or 'None'
    payload = _text(json.dumps(graph['entities'], ensure_ascii=True, indent=2, allow_nan=False))
    return (
        f'<article id="row-{index}" class="record"><h3>Row {index}</h3>'
        f'<p><span class="state">{_text(_STATES[row["internal_status"]])}</span> · '
        f'Stage 1 probability {_text(row["stage1_probability"])} · Calls {_text(labels)}</p>'
        f'<details><summary>Trace row {index}</summary>{_pipeline(graph)}{_relationships(graph)}'
        f'<h3>Entities</h3><pre class="payload">{payload}</pre></details></article>'
    )


def _page_html(graph, first, last, previous, following):
    links = ['<a href="index.html">Batch overview</a>', f'<a href="rows-{first}.json">Page JSON</a>']
    if previous is not None:
        links.append(f'<a rel="prev" href="rows-{previous}.html">Previous page</a>')
    if following is not None:
        links.append(f'<a rel="next" href="rows-{following}.html">Next page</a>')
    title = f'Records {first}–{last - 1}'
    body = f'<h1>{title}</h1><nav aria-label="Record pages">{"".join(links)}</nav>'
    body += ''.join(_record_html(row) for row in graph['records'])
    return _html(title, body)


def _json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=True, indent=2, allow_nan=False) + '\n',
                    encoding='utf-8', newline='\n')


def _write_export(evidence, output, start, stop, page_size):
    total = len(evidence.records)
    if stop is None:
        stop = min(total, start + page_size)
    if type(stop) is not int or not start < stop <= total:
        raise ValueError('Select a nonempty row range within the verified result')
    graph = file_graph(evidence)
    graph['selection'] = {'start': start, 'stop': stop, 'page_size': page_size}
    _json(output / 'lineage.json', graph)
    (output / 'index.html').write_text(_overview_html(graph), encoding='utf-8', newline='\n')
    for first in range(start, stop, page_size):
        last = min(first + page_size, stop)
        previous = first - page_size if first > start else None
        following = last if last < stop else None
        page = {'schema_version': graph['schema_version'], 'kind': 'record_page', 'identity': graph['identity'],
                'start': first, 'stop': last, 'record_count': total,
                'records': [record_graph(evidence, row) for row in range(first, last)]}
        _json(output / f'rows-{first}.json', page)
        (output / f'rows-{first}.html').write_text(_page_html(page, first, last, previous, following),
                                                   encoding='utf-8', newline='\n')
    return graph


def export_lineage(directory, destination, *, start=0, stop=None, page_size=100,
                   expected_manifest_sha256=None, reference_paths=None):
    """Publish a file graph and paged record graphs in JSON and offline HTML.

    The row range is zero-based with an exclusive stop. With no stop, export
    one page starting at the requested row. Each page contains at most 100 rows.
    """
    if type(start) is not int or start < 0 or type(page_size) is not int or not 1 <= page_size <= 100:
        raise ValueError('Use a nonnegative start and a page_size between 1 and 100')
    root, target = artifact_path(directory), artifact_path(destination)
    if target.is_relative_to(root):
        raise ValueError('Lineage destination must be outside the verified source')
    if target.is_relative_to(ROOT) and not target.is_relative_to(ROOT / 'results'):
        raise ValueError('Lineage exports within the software root must be under results/')
    if INSTALLED and any(target.is_relative_to(CODE_ROOT / name) for name in ('incfinder', 'ntv3_tsrf')):
        raise ValueError('Lineage exports must not be written into the installed package')
    require_new_output(target)
    evidence = load_evidence(root, expected_manifest_sha256=expected_manifest_sha256, reference_paths=reference_paths)
    with reserve_output(target, evidence.identity['run_id']), TemporaryDirectory(prefix='.incfinder-lineage-', dir=target.parent) as temporary:
        staged = Path(temporary) / 'export'
        staged.mkdir()
        result = _write_export(evidence, staged, start, stop, page_size)
        publish_output(staged, target)
    return result
