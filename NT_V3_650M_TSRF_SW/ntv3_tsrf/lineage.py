"""Inspect file or record lineage for an already published prediction."""
from types import SimpleNamespace

from .artifact_lifecycle import retained_view
from .lineage_evidence import load_evidence


_SCHEMA = 1
_FILE_STAGES = {
    'embedding': 'Encode features', 'classification': 'Classify records',
    'report': 'Write the workbook', 'publication': 'Publish results',
}
_POLICY = {
    'scientific': ('Scientific contract', 'contract_sha256'),
    'runtime': ('Runtime policy', 'runtime_policy_sha256'),
    'output': ('Output contract', 'output_contract_sha256'),
    'assets': ('Frozen asset manifest', 'model_manifest_sha256'),
}
_MODELS = {
    'encoder': ('NTv3 encoder weights', 'models/ntv3/model.safetensors'),
    'stage1': ('Stage 1 random forest', 'models/rf/stage1.joblib'),
    'stage2': ('Stage 2 random forest', 'models/rf/stage2.joblib'),
}


class _Graph:
    """Store identified entities, observed activities and their PROV relationships."""

    def __init__(self, evidence, kind):
        self.evidence, self.kind = evidence, kind
        self.entities, self.activities, self.relations = {}, {}, []

    def key(self, name):
        return f"{self.evidence.identity['run_id']}:{name}"

    def entity(self, key, label, **attributes):
        self.entities.setdefault(key, {'id': key, 'label': label, **attributes})
        return key

    def artifact(self, artifact):
        role = artifact['role']
        path = artifact['path'] if artifact['storage'] != 'reference' else None
        identity = artifact['artifact_id']
        missing = self.evidence.unavailable or {}
        availability = {'availability': 'unavailable', 'availability_reason': missing[identity]} if identity in missing else {}
        return self.entity(identity, path or role.replace('_', ' ').capitalize(),
                           kind='file', role=role, relative_path=path, size_bytes=artifact['size_bytes'],
                           sha256=artifact['sha256'], storage=artifact['storage'], **availability)

    def activity(self, name, label, stage, observed_by):
        key = self.key('activity/' + name)
        attributes = {'id': key, 'label': label, 'stage': stage, 'evidence': [observed_by]}
        if stage in self.evidence.stages:
            attributes['stage_recorded_at_utc'] = self.evidence.stages[stage]
            attributes['evidence'].append('run_events')
        self.activities.setdefault(key, attributes)
        return key

    def use(self, activity, entity):
        edge = {'type': 'used', 'activity': activity, 'entity': entity}
        if edge not in self.relations:
            self.relations.append(edge)

    def derive(self, child, parent):
        self.relations.append({'type': 'wasDerivedFrom', 'generated_entity': child, 'used_entity': parent})

    def generate(self, activity, output, inputs):
        self.relations.append({'type': 'wasGeneratedBy', 'entity': output, 'activity': activity})
        for item in inputs:
            self.use(activity, item)
            self.derive(output, item)

    def policy(self, key):
        label, field = _POLICY[key]
        return self.entity(self.key('policy/' + key), label, kind='policy', sha256=self.evidence.identity[field])

    def model(self, key):
        label, asset = _MODELS[key]
        model = self.entity(self.key('model/' + key), label, kind='model', asset=asset,
                            sha256=self.evidence.assets[asset])
        if not any(r.get('generated_entity') == model for r in self.relations):
            self.derive(model, self.policy('assets'))
        return model

    def gate_threshold(self):
        return self.entity(self.key('policy/stage1-threshold'), 'Stage 1 gate threshold', kind='policy',
                           operator='>=', threshold=self.evidence.context['stage1_threshold'])

    def label_thresholds(self):
        context = self.evidence.context
        props = {'kind': 'policy', 'operator': '>=', 'threshold_mode': context['threshold_mode'],
                 'label_order': context['label_order'], 'thresholds': context['stage2_thresholds']}
        if context['threshold_mode'] == 'ct':
            props.update(asset='config/thresholds.tsv', sha256=self.evidence.assets['config/thresholds.tsv'])
        return self.entity(self.key('policy/stage2-thresholds'), 'Stage 2 label thresholds', **props)

    def result(self, **attributes):
        return {
            'schema_version': _SCHEMA, 'kind': self.kind, 'identity': self.evidence.identity,
            'verification': {'manifest_evidence': self.evidence.manifest_evidence,
                             'source_rows_and_csv_match': True, 'probabilities_and_calls_match': True},
            **attributes, 'entities': list(self.entities.values()), 'activities': list(self.activities.values()),
            'relations': self.relations,
        }


def _artifact_edges(graph, entries):
    for item in entries:
        graph.artifact(item)
    for item in entries:
        key, stage, parents = item['artifact_id'], item['stage'], item['parents']
        if stage == 'source':
            for parent in parents:
                graph.derive(key, parent)
            continue
        activity = graph.activity(stage, _FILE_STAGES[stage], stage, 'artifact_manifest')
        graph.generate(activity, key, parents)


def file_graph(evidence):
    graph = _Graph(evidence, 'file_lineage')
    _artifact_edges(graph, evidence.manifest['artifacts'])
    for stage in ('embedding', 'classification'):
        activity = graph.key('activity/' + stage)
        if activity not in graph.activities:
            continue
        graph.use(activity, graph.model('encoder' if stage == 'embedding' else 'stage1'))
        graph.use(activity, graph.policy('scientific'))
        graph.use(activity, graph.policy('runtime'))
        if stage == 'classification':
            graph.use(activity, graph.gate_threshold())
            graph.use(activity, graph.policy('output'))
            if evidence.status_counts['Unclassified'] < len(evidence.records):
                graph.use(activity, graph.model('stage2'))
                graph.use(activity, graph.label_thresholds())
    return graph.result(record_count=len(evidence.records), status_counts=evidence.status_counts)


def record_graph(evidence, row_index):
    if type(row_index) is not int or not 0 <= row_index < len(evidence.records):
        raise ValueError('row_index must identify a zero-based record in the verified result')
    graph = _Graph(evidence, 'record_lineage')
    data, context, record = evidence.arrays, evidence.context, evidence.records[row_index]
    inputs = evidence.inputs
    prefix = f'row/{row_index}'
    source = graph.artifact(inputs['input'])
    features = graph.artifact(inputs['features'])
    feature_meta = graph.artifact(inputs['feature_metadata'])
    bindings = {entry['path']: graph.artifact(entry) for entry in evidence.manifest['artifacts']
                if entry['path'] in ('inc_results.csv', 'probabilities.npz')}
    sequence = graph.entity(graph.key(prefix + '/input'), 'Source record', kind='record', row_index=row_index,
                            length_bp=record['length_bp'], sequence_sha256=record['sequence_sha256'])
    graph.derive(sequence, source)
    feature = graph.entity(graph.key(prefix + '/feature'), 'Feature row', kind='record', row_index=row_index,
                           artifact_id=features, dtype='float32', width=1536, origin=inputs['features']['stage'])
    if inputs['features']['stage'] == 'embedding':
        action = graph.activity(prefix + '/encoding', 'Encode feature row', 'embedding', 'retained_features')
        graph.generate(action, feature, (sequence, graph.model('encoder'), graph.policy('scientific'),
                                        graph.policy('runtime')))
        graph.derive(feature, features)
        graph.derive(feature, feature_meta)
    else:
        action = graph.activity(prefix + '/feature', 'Read verified precomputed feature row', 'source', 'feature_metadata')
        graph.generate(action, feature, (sequence, features, feature_meta))
    stage1_value = float(data['stage1'][row_index])
    passed = bool(data['gate'][row_index])
    stage1 = graph.entity(graph.key(prefix + '/stage1'), 'Stage 1 probability', kind='record',
                          probability=stage1_value, artifact_id=bindings['probabilities.npz'])
    action = graph.activity(prefix + '/stage1', 'Compute Stage 1 probability', 'classification', 'verified_probabilities')
    graph.generate(action, stage1, (feature, graph.model('stage1'), graph.policy('scientific'),
                                   graph.policy('runtime')))
    gate = graph.entity(graph.key(prefix + '/gate'), 'Stage 1 gate decision', kind='record', passed=passed)
    action = graph.activity(prefix + '/gate', 'Apply Stage 1 gate', 'classification', 'verified_gate')
    graph.generate(action, gate, (stage1, graph.gate_threshold()))
    parents = [gate, graph.policy('output')]
    stage2_status = 'not_run_gate_rejected'
    if passed:
        stage2_status = 'computed'
        stage2 = graph.entity(graph.key(prefix + '/stage2'), 'Stage 2 probabilities', kind='record',
                              label_order=context['label_order'], probabilities=data['stage2'][row_index].tolist(),
                              artifact_id=bindings['probabilities.npz'])
        action = graph.activity(prefix + '/stage2', 'Compute Stage 2 probabilities', 'classification', 'verified_probabilities')
        graph.generate(action, stage2, (feature, gate, graph.model('stage2'), graph.policy('scientific'),
                                       graph.policy('runtime')))
        parents.extend((stage2, graph.label_thresholds()))
    state = str(data['internal_status'][row_index])
    labels = [name for name, value in zip(context['label_order'], data['labels'][row_index]) if value]
    result = graph.entity(graph.key(prefix + '/result'), 'Final result row', kind='record', row_index=row_index,
                          artifact_id=bindings['inc_results.csv'], internal_status=state, called_labels=labels,
                          stage2_status=stage2_status)
    action = graph.activity(prefix + '/result', 'Apply the output contract', 'classification', 'verified_csv_and_calls')
    graph.generate(action, result, parents)
    graph.derive(result, bindings['inc_results.csv'])
    return graph.result(row_index=row_index, row={'internal_status': state, 'gate_pass': passed,
                                                 'stage1_probability': stage1_value, 'stage2_status': stage2_status,
                                                 'called_labels': labels})


def _retained_file_graph(view):
    events, _, _ = view.publication
    stages = {event['payload']['stage']: event['recorded_utc'] for event in events
              if event['event_type'] == 'stage_entered'}
    identity = {'run_id': view.manifest['run_id'], 'manifest_kind': view.manifest['kind'],
                'manifest_sha256': view.digest, 'model_manifest_sha256': view.manifest['model_manifest_sha256']}
    evidence = SimpleNamespace(identity=identity, stages=stages, unavailable=view.missing,
                               manifest_evidence=['detached_manifest_sha256', 'publication_event_sha256'])
    graph = _Graph(evidence, 'file_lineage')
    _artifact_edges(graph, view.manifest['artifacts'])
    for key, item in graph.entities.items():
        reason = view.missing.get(key)
        item['availability'] = 'unavailable' if reason else 'available'
        if reason:
            item['availability_reason'] = reason
        if reason == 'retention':
            event = view.retention[key]
            item['retention'] = {k: event[k] for k in ('status', 'policy', 'intent_utc', 'recorded_utc', 'reason')
                                 if k in event}
    result = graph.result()
    result['verification'].update(source_rows_and_csv_match=False, probabilities_and_calls_match=False)
    return result


def inspect_lineage(directory, *, row_index=None, expected_manifest_sha256=None, reference_paths=None):
    """Return the verified file graph, or the graph for one zero-based output row."""
    if row_index is not None and (type(row_index) is not int or row_index < 0):
        raise ValueError('row_index must be a nonnegative integer')
    view = retained_view(directory, expected_manifest_sha256=expected_manifest_sha256, reference_paths=reference_paths)
    if view and row_index is None:
        return _retained_file_graph(view)
    evidence = load_evidence(directory, expected_manifest_sha256=expected_manifest_sha256,
                             reference_paths=reference_paths, artifact_view=view)
    return file_graph(evidence) if row_index is None else record_graph(evidence, row_index)
