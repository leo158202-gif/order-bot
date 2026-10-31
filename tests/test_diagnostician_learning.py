from copy import deepcopy
from types import SimpleNamespace

import pytest
import yaml

from app.db import init_db
from app.diagnostician.store import DoctorStore
from app.feishu.sheet_writer import _digest


BEFORE = {"receiver": "合成客户", "phone": "", "address": "", "source_msgids": ["synthetic-source"],
          "lines": [{"raw": "30*30黑色扶手2支6米", "kind": "rail", "product": "30黑扶手", "config": "2支6米", "qty": "12米"}]}
AFTER = {**BEFORE, "lines": [{**BEFORE["lines"][0], "product": "30*30黑色铝合金卡槽扶手"}]}
OLD = {"lines": [{"raw": "40*40灰色扶手2支6米", "kind": "rail", "product": "40*40灰色铝合金卡槽扶手", "config": "2支6米", "qty": "12米"}]}


def test_learning_conflict_remains_pending_in_real_validator():
    from app.diagnostician.learning import _review
    from app.pipeline.validator import validate
    order = deepcopy(BEFORE)
    order.update(phone='19900000003', address='合成地址')
    _review(order, '金标准有限样例冲突，保留待核')
    state, reasons = validate(order)
    assert state == 'pending'
    assert any('有限样例冲突' in reason for reason in reasons)


@pytest.fixture
def setup_learning(tmp_path):
    from app.diagnostician.learning import LearningService
    from app.diagnostician.versions import VersionService
    from app.pipeline.normalizer import SYSTEM_TEMPLATE
    root = tmp_path / "data"
    root.mkdir()
    root.joinpath("rules.yaml").write_text("normalize: {}\nrails: {mapping: []}\nposts: {}\nchannels: {}\nfittings: {}\nauto_calc: {}\nline_order: []\n", encoding="utf-8")
    dbpath = tmp_path / "business.sqlite"
    store = DoctorStore(tmp_path / "agent.sqlite")
    versions = VersionService(store, root, init_db(dbpath), database=dbpath)
    fixed = [{"id": "old-correct-40-gray", "input": OLD, "expected": OLD}]
    learning = LearningService(store, versions, prompt_template=SYSTEM_TEMPLATE, fixed_cases=fixed)
    versions.evidence_validator = learning.evidence_validator
    versions.regression_validator = learning.regression_validator
    context = SimpleNamespace(session_id="owner", task_id=None, request_id="request", target_id="test", capabilities=frozenset({"versions_write"}))
    return store, versions, learning, context


def correction(store, identity="correction-1", before=None, after=None, phase="verified"):
    manifest = {"action_id": identity, "target_id": "test", "before_data": deepcopy(before or BEFORE),
                "after_data": deepcopy(after or AFTER), "evidence_refs": ["feedback:synthetic", "source:synthetic"],
                "base_hashes": {"code": "old-code", "rules": "old-rules", "prompt": "old-prompt"}}
    manifest["digest"] = _digest(manifest)
    store.put_record("owner", "correction", identity, {"phase": phase, "manifest": manifest})


def test_verified_correction_prepares_only_exact_scoped_rule_and_old_cases_pass(setup_learning):
    from app.diagnostician.learning import apply_learned_cases
    store, versions, learning, ctx = setup_learning
    correction(store)
    prepared = learning.prepare_from_correction("correction-1", ctx)
    assert prepared["status"] == "ok"
    version = versions._get(prepared["data"]["version_id"])["payload"]
    rules = yaml.safe_load(version["text"])
    assert rules["rails"] == {"mapping": []}
    case = rules["scoped_learned_cases"][0]
    assert case["match"]["spec"] == "30*30"
    assert case["match"]["color"] == "黑色"
    assert apply_learned_cases(BEFORE, rules)["lines"] == AFTER["lines"]
    assert apply_learned_cases(OLD, rules) == OLD
    assert BEFORE["lines"][0]["product"] == "30黑扶手"
    for field, value in (("qty", "18米"), ("raw", "30*30灰色扶手2支6米"), ("config", "3支6米")):
        changed = deepcopy(BEFORE)
        changed["lines"][0][field] = value
        assert apply_learned_cases(changed, rules) == changed
    validated = versions.validate_version(version["version_id"], ctx)
    assert validated["status"] == "ok"
    assert any(name.startswith("old:") for name in validated["data"]["validation"]["tests"])
    assert any(name.startswith("new:") for name in validated["data"]["validation"]["tests"])


def test_contact_change_or_unverified_manifest_never_becomes_global_rule(setup_learning):
    store, versions, learning, ctx = setup_learning
    changed = deepcopy(BEFORE)
    changed["phone"] = "19900000011"
    correction(store, "contact", BEFORE, changed)
    assert learning.prepare_from_correction("contact", ctx)["status"] == "needs_input"
    correction(store, "pending", phase="partial")
    assert learning.prepare_from_correction("pending", ctx)["status"] == "needs_input"
    correction(store, "tampered")
    saved = store.get_record("owner", "correction", "tampered")
    saved["payload"]["manifest"]["after_data"]["lines"][0]["qty"] = "100米"
    store.put_record("owner", "correction", "tampered", saved["payload"])
    assert learning.prepare_from_correction("tampered", ctx)["status"] == "needs_input"
    assert versions.list_versions() == []


def test_model_yaml_changes_and_scope_expansion_cannot_pass_evidence_validator(setup_learning):
    store, versions, learning, ctx = setup_learning
    correction(store)
    prepared = learning.prepare_from_correction("correction-1", ctx)
    candidate = versions._get(prepared["data"]["version_id"])["payload"]
    assert learning.evidence_validator("rules", candidate) is True
    forged = deepcopy(candidate)
    forged["text"] = forged["text"].replace("normalize: {}", "normalize: {default_color: 黑色}")
    assert learning.evidence_validator("rules", forged) is False
    forged = deepcopy(candidate)
    forged["scope"]["spec"] = "*"
    assert learning.evidence_validator("rules", forged) is False


def test_missing_fixed_regressions_keeps_candidate_unpublished(setup_learning):
    from app.diagnostician.learning import LearningService
    from app.pipeline.normalizer import SYSTEM_TEMPLATE
    store, versions, _, ctx = setup_learning
    learning = LearningService(store, versions, prompt_template=SYSTEM_TEMPLATE, fixed_cases=[])
    versions.evidence_validator, versions.regression_validator = learning.evidence_validator, learning.regression_validator
    correction(store)
    prepared = learning.prepare_from_correction("correction-1", ctx)
    assert versions.validate_version(prepared["data"]["version_id"], ctx)["status"] == "failed"
    assert versions.current_text("rules").find("scoped_learned_cases") == -1


def test_conflicting_old_correct_fixture_prevents_automatic_publication(setup_learning):
    from app.diagnostician.learning import LearningService
    from app.pipeline.normalizer import SYSTEM_TEMPLATE
    store, versions, _, ctx = setup_learning
    learning = LearningService(store, versions, prompt_template=SYSTEM_TEMPLATE,
        fixed_cases=[{"id": "contradicting-old-correct", "input": BEFORE, "expected": BEFORE}])
    versions.evidence_validator, versions.regression_validator = learning.evidence_validator, learning.regression_validator
    correction(store)
    prepared = learning.prepare_from_correction("correction-1", ctx)
    assert versions.validate_version(prepared["data"]["version_id"], ctx)["status"] == "failed"


def test_prompt_learning_preserves_entire_core_template_and_format_placeholders(setup_learning):
    from app.pipeline.normalizer import SYSTEM_TEMPLATE
    store, versions, learning, ctx = setup_learning
    correction(store)
    prepared = learning.prepare_from_correction("correction-1", ctx, kind="prompt")
    candidate = versions._get(prepared["data"]["version_id"])["payload"]
    assert candidate["text"].startswith(SYSTEM_TEMPLATE)
    formatted = candidate["text"].format(rules_text="safe-rules", context="safe-context")
    assert "safe-rules" in formatted and "safe-context" in formatted
    assert versions.validate_version(candidate["version_id"], ctx)["status"] == "ok"
    altered = deepcopy(candidate)
    altered["text"] = altered["text"].replace("禁止引用上下文编号", "允许引用上下文编号")
    assert learning.evidence_validator("prompt", altered) is False


def test_unique_saved_gold_difference_can_create_scoped_case_without_rescanning(setup_learning):
    store, versions, learning, ctx = setup_learning
    difference = {"gold_index": 0, "order_id": 7, "source_digest": "source-v1", "gold_digest": "gold-cell-v1",
                  "source_day": "2026-10-27", "rows": [2], "observed": BEFORE, "expected": AFTER}
    report = {"differences": [difference], "ambiguous": [], "gold_only": [], "first_metrics": {"numerator": 0, "denominator": 1}}
    store.put_record("owner", "gold-reports", "attachment", report)
    prepared = learning.prepare_from_gold("attachment", 0, ctx)
    assert prepared["status"] == "ok"
    assert store.get_record("owner", "gold-reports", "attachment")["payload"] == report
    assert versions.validate_version(prepared["data"]["version_id"], ctx)["status"] == "ok"


def test_ambiguous_gold_or_missing_source_attributes_stays_candidate_only(setup_learning):
    store, versions, learning, ctx = setup_learning
    store.put_record("owner", "gold-reports", "attachment", {"differences": [], "ambiguous": [{"gold_index": 0}]})
    assert learning.prepare_from_gold("attachment", 0, ctx)["status"] == "needs_input"
    unclear = deepcopy(BEFORE)
    unclear["lines"][0]["raw"] = "扶手2支6米"
    fixed = deepcopy(unclear)
    fixed["lines"][0]["product"] = "厚款扶手"
    correction(store, "missing-scope", unclear, fixed)
    assert learning.prepare_from_correction("missing-scope", ctx)["status"] == "needs_input"


def test_scoped_rule_publish_and_rollback_preserve_fixed_regressions(setup_learning):
    store, versions, learning, ctx = setup_learning
    correction(store)
    prepared = learning.prepare_from_correction("correction-1", ctx)
    identity = prepared["data"]["version_id"]
    assert versions.validate_version(identity, ctx)["status"] == "ok"
    assert versions.publish_version(identity, versions.current_hash("rules"), ctx)["status"] == "ok"
    assert "scoped_learned_cases" in versions.current_text("rules")
    # Rollback to the same proven version still creates an auditable version.
    ctx.request_id = "rollback"
    assert versions.rollback_version(identity, ctx)["status"] == "ok"


def test_formula_candidate_and_personal_raw_are_not_executable_rules(setup_learning):
    store, versions, learning, ctx = setup_learning
    formula = deepcopy(AFTER)
    formula["lines"][0]["qty"] = "=SUM(1,2)"
    correction(store, "formula", BEFORE, formula)
    assert learning.prepare_from_correction("formula", ctx)["status"] == "needs_input"
    personal = deepcopy(BEFORE)
    personal["lines"][0]["raw"] += " 联系电话19900000011"
    updated = deepcopy(personal)
    updated["lines"][0]["product"] = AFTER["lines"][0]["product"]
    correction(store, "personal", personal, updated)
    assert learning.prepare_from_correction("personal", ctx)["status"] == "needs_input"


def test_changed_evidence_invalidates_unpublished_candidate_and_other_session_cannot_read_it(setup_learning):
    store, versions, learning, ctx = setup_learning
    correction(store)
    prepared = learning.prepare_from_correction("correction-1", ctx)
    saved = store.get_record("owner", "correction", "correction-1")
    saved["payload"]["phase"] = "partial"
    store.put_record("owner", "correction", "correction-1", saved["payload"])
    assert versions.validate_version(prepared["data"]["version_id"], ctx)["status"] == "needs_input"
    ctx.session_id = "stranger"
    assert learning.prepare_from_correction("correction-1", ctx)["status"] == "needs_input"


def test_conflicting_exact_cases_preserve_old_value_and_existing_review_notes(setup_learning):
    from app.diagnostician.learning import apply_learned_cases
    store, versions, learning, ctx = setup_learning
    correction(store)
    prepared = learning.prepare_from_correction("correction-1", ctx)
    rules = yaml.safe_load(versions._get(prepared["data"]["version_id"])["payload"]["text"])
    conflict = deepcopy(rules["scoped_learned_cases"][0])
    conflict["id"] = "different-case"
    conflict["patch"]["product"] = "冲突写法"
    rules["scoped_learned_cases"].append(conflict)
    original = {**BEFORE, "review_reasons": ["已有备注保留"]}
    applied = apply_learned_cases(original, rules)
    assert applied["lines"] == BEFORE["lines"]
    assert applied["review_reasons"][0] == "已有备注保留"
    assert any("冲突" in reason for reason in applied["review_reasons"])


def test_malformed_learned_data_adds_review_instead_of_crashing_pipeline():
    from app.diagnostician.learning import apply_learned_cases
    malformed = {"id": "broken", "match": ["raw", "product", "config", "qty", "spec", "color"],
                 "patch": {"product": "bad"}, "evidence_refs": ["x"], "exceptions": []}
    original = {**BEFORE, "review_reasons": "旧备注"}
    output = apply_learned_cases(original, {"scoped_learned_cases": [malformed]})
    assert output["lines"] == BEFORE["lines"]
    assert output["review_reasons"][0] == "旧备注"
    assert any("结构异常" in reason for reason in output["review_reasons"])
