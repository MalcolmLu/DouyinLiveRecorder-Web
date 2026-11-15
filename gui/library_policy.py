"""Stable library decisions shared by the worker and its read-only console."""
import json


def retention_details(raw):
    try:
        proof = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        proof = None
    if not isinstance(proof, dict) or proof.get('decision') != 'retained_original':
        return {'retained_original': False, 'candidate_size': None}
    size = proof.get('candidate_size')
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        size = None
    return {'retained_original': True, 'candidate_size': size}
