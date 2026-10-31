"""Target eligibility is host-owned; it never supplies a task capability."""
from app import deployment


def target_allowed(target_id, data_root=None):
    if deployment.active():
        return deployment.current_target_allowed(target_id, data_root)
    return target_id == 'test'
