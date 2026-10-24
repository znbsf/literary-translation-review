"""Private, versioned series proposals and reproducible per-book run manifests.

This module does not dispatch models or certify proposals. Full-series background
is available; zero-based horizons label revelation constraints and scene scope.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import re

from .storage import digest, private_root, project_lock, write_json


class RegistryError(ValueError):
    pass


def _require(test, code):
    if not test:
        raise RegistryError(code)


def _id(value):
    _require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value), "invalid_id")
    return value


def _read(path):
    private_root(path.parent)
    _require(not path.is_symlink() and not path.is_junction() and path.stat().st_size < 20_000_000,
             "invalid_private_file")
    return json.loads(path.read_text(encoding="utf-8"))


def _state(root):
    state = _read(private_root(root) / "series.json")
    _require(state.get("format") == "translation-series-v1", "invalid_registry")
    return state


def initialize(root: Path, series_id: str):
    with project_lock(root):
        _require(not (root / "series.json").exists(), "already_initialized")
        state = {"format": "translation-series-v1", "series_id": _id(series_id),
                 "books": {}, "profiles": {}, "backgrounds": {}, "runs": {}}
        write_json(root / "series.json", state)
    return state


def _refresh(state):
    for run in state["runs"].values():
        reasons = []
        if state["books"][run["book_id"]]["source_digest"] != run["source_digest"]:
            reasons.append("source_changed")
        for dep in run["dependencies"]:
            current = state["profiles"].get(dep["profile_id"])
            if current is None or current["revision"] != dep["revision"]:
                reasons.append("profile_changed")
            for book_id, expected in dep["source_dependencies"].items():
                if state["books"][book_id]["source_digest"] != expected:
                    reasons.append("evidence_source_changed")
        background_dependencies = run.get("background_dependencies", [])
        if {dep["background_id"] for dep in background_dependencies} != set(state.get("backgrounds", {})):
            reasons.append("background_catalogue_changed")
        for dep in background_dependencies:
            current = state.get("backgrounds", {}).get(dep["background_id"])
            if current is None or current["revision"] != dep["revision"]:
                reasons.append("background_changed")
            if state["books"][dep["book_id"]]["source_digest"] != dep["source_digest"]:
                reasons.append("background_source_changed")
        run["status"] = "stale" if reasons else "ready"
        run["stale_reasons"] = sorted(set(reasons))


def register_book(root, book_id, source_digest, publication_order, paragraph_count, *, order_basis="publication"):
    """Register source metadata and a stable traversal position.

    ``publication_order`` retains its legacy field name. When ``order_basis`` is
    ``corpus_traversal``, the number is only corpus traversal order, never a claim
    about publication dates or the chronology of the story.
    """
    _id(book_id)
    _require(isinstance(source_digest, str) and re.fullmatch("[0-9a-f]{64}", source_digest), "invalid_digest")
    _require(type(publication_order) is int and publication_order >= 1 and
             type(paragraph_count) is int and paragraph_count >= 1, "invalid_book_order")
    _require(order_basis in {"publication", "corpus_traversal"}, "invalid_order_basis")
    with project_lock(root):
        state = _state(root)
        _require(all(k == book_id or v["publication_order"] != publication_order
                     for k, v in state["books"].items()), "duplicate_book_order")
        old = state["books"].get(book_id)
        _require(old is None or old["publication_order"] == publication_order, "book_order_immutable")
        state["books"][book_id] = {"source_digest": source_digest,
            "publication_order": publication_order, "paragraph_count": paragraph_count,
            "order_basis": order_basis}
        _refresh(state)
        write_json(root / "series.json", state)
    return state["books"][book_id]


def _position(state, book_id, paragraph):
    """Relative scope position only; never truncate full-series background.

    The order may be corpus traversal and must not establish narrative chronology.
    """
    _require(book_id in state["books"], "unknown_book")
    book = state["books"][book_id]
    _require(type(paragraph) is int and 0 <= paragraph < book["paragraph_count"], "invalid_horizon")
    return book["publication_order"], paragraph


def _validate_rules(state, rules):
    _require(isinstance(rules, list) and bool(rules), "missing_rules")
    ids = []
    dependencies = {}
    for rule in rules:
        ids.append(_id(rule["id"]))
        _require(rule.get("scope") in {"author_candidate", "series_candidate", "character", "scene"}, "invalid_scope")
        _require(isinstance(rule.get("rule"), str) and rule["rule"].strip(), "missing_rule")
        at = rule["known_at"]
        _position(state, at["book_id"], at["paragraph"])
        dependencies[at["book_id"]] = state["books"][at["book_id"]]["source_digest"]
        if rule["scope"] == "character":
            _id(rule.get("character_id"))
        if rule["scope"] == "scene":
            _require(type(rule.get("end_paragraph")) is int and
                     at["paragraph"] <= rule["end_paragraph"] < state["books"][at["book_id"]]["paragraph_count"], "invalid_scene_end")
    _require(len(ids) == len(set(ids)), "duplicate_rule_id")
    return dependencies


def _save_profile(root, state, profile_id, rules, provenance, status, parent=None):
    revision = state["profiles"].get(profile_id, {}).get("revision", 0) + 1
    value = {"profile_id": profile_id, "revision": revision, "status": status,
             "rules": deepcopy(rules), "provenance": deepcopy(provenance),
             "source_dependencies": _validate_rules(state, rules), "parent": parent}
    path = root / "profiles" / profile_id / f"revision-{revision}.json"
    _require(not path.exists(), "revision_exists")
    write_json(path, value)
    state["profiles"][profile_id] = {"revision": revision, "digest": digest(value)}
    _refresh(state)
    write_json(root / "series.json", state)
    return value


def register_profile(root, profile_id, rules, provenance):
    """Every imported/model revision is proposed, even if its input says otherwise."""
    _id(profile_id)
    with project_lock(root):
        return _save_profile(root, _state(root), profile_id, rules, provenance, "proposed")


def _profile(root, state, profile_id):
    head = state["profiles"][profile_id]
    value = _read(root / "profiles" / profile_id / f"revision-{head['revision']}.json")
    _require(digest(value) == head["digest"], "profile_changed_on_disk")
    return value


def register_background(root, background_id, book_id, content):
    """Persist a private model proposal; never follow links contained in content."""
    _id(background_id)
    _require(isinstance(content, dict) and bool(content), "invalid_background")
    with project_lock(root):
        state = _state(root)
        _require(book_id in state["books"], "unknown_book")
        heads = state.setdefault("backgrounds", {})
        revision = heads.get(background_id, {}).get("revision", 0) + 1
        value = {"background_id": background_id, "revision": revision,
                 "book_id": book_id, "source_digest": state["books"][book_id]["source_digest"],
                 "status": "proposed", "content": deepcopy(content)}
        path = root / "backgrounds" / background_id / f"revision-{revision}.json"
        _require(not path.exists(), "revision_exists")
        write_json(path, value)
        heads[background_id] = {"revision": revision, "digest": digest(value)}
        _refresh(state)
        write_json(root / "series.json", state)
    return value


def _background(root, state, background_id):
    head = state.get("backgrounds", {})[background_id]
    value = _read(root / "backgrounds" / background_id / f"revision-{head['revision']}.json")
    _require(digest(value) == head["digest"], "background_changed_on_disk")
    return value


def accept_profile(root, profile_id, *, explicit_user_acceptance=False):
    _require(explicit_user_acceptance is True, "explicit_user_acceptance_required")
    _id(profile_id)
    with project_lock(root):
        state = _state(root)
        old = _profile(root, state, profile_id)
        _require(all(state["books"][k]["source_digest"] == v for k, v in old["source_dependencies"].items()), "stale_profile")
        return _save_profile(root, state, profile_id, old["rules"],
            {"operation": "explicit_user_acceptance", "prior_provenance": old["provenance"]},
            "confirmed", old["revision"])


def create_run(root, run_id, book_id, horizon, *, experimental=False, character_ids=()):
    _id(run_id)
    with project_lock(root):
        state = _state(root)
        target = _position(state, book_id, horizon)
        _require(run_id not in state["runs"], "run_exists")
        selected, dependencies = [], []
        for profile_id in state["profiles"]:
            profile = _profile(root, state, profile_id)
            if profile["status"] != "confirmed" and not experimental:
                continue
            if any(state["books"][k]["source_digest"] != v for k, v in profile["source_dependencies"].items()):
                continue
            eligible = []
            for rule in profile["rules"]:
                at = rule["known_at"]
                future = _position(state, at["book_id"], at["paragraph"]) > target
                if rule["scope"] == "scene" and (at["book_id"] != book_id or
                        not at["paragraph"] <= horizon <= rule["end_paragraph"]):
                    continue
                if rule["scope"] == "character" and rule["character_id"] not in character_ids:
                    continue
                eligible.append({**deepcopy(rule), "profile_id": profile_id,
                                 "profile_revision": profile["revision"], "review_status": profile["status"],
                                 "revelation_constraint": "background_only_do_not_add_to_translation" if future else "follow_target_source"})
            if eligible:
                selected.extend(eligible)
                dependencies.append({"profile_id": profile_id, "revision": profile["revision"],
                                     "source_dependencies": profile["source_dependencies"]})
        backgrounds = []
        for background_id in state.get("backgrounds", {}):
            background = _background(root, state, background_id)
            _require(state["books"][background["book_id"]]["source_digest"] == background["source_digest"],
                     "stale_background")
            backgrounds.append({"background_id": background_id, "revision": background["revision"],
                                "book_id": background["book_id"], "source_digest": background["source_digest"],
                                "digest": digest(background)})
        manifest = {"format": "translation-series-run-v1", "run_id": run_id,
                    "book_id": book_id, "source_digest": state["books"][book_id]["source_digest"],
                    "horizon": horizon, "experimental": bool(experimental), "rules": selected,
                    "dependencies": dependencies, "status": "ready", "stale_reasons": [],
                    "background_dependencies": backgrounds,
                    "full_series_context": True,
                    "exclude_target_translation_examples": True,
                    "example_policy": "allow_nonoverlapping_source_and_translation_examples",
                    "translation_acceptance": "not_accepted"}
        write_json(root / "runs" / f"{run_id}.json", manifest)
        state["runs"][run_id] = {k: v for k, v in manifest.items() if k != "rules"}
        state["runs"][run_id]["manifest_digest"] = digest(manifest)
        write_json(root / "series.json", state)
    return manifest


def run_status(root, run_id):
    """Live dependency status; immutable run file records the original inputs."""
    state = _state(root)
    _refresh(state)
    return deepcopy(state["runs"][_id(run_id)])


def build_context_pack(root, run_id):
    """Load only a recorded immutable manifest, rejecting stale/tampered inputs.

    No paths from the manifest are followed. A successful check applies to this
    snapshot; callers must rebuild immediately before a later model dispatch.
    """
    _id(run_id)
    with project_lock(root):
        state = _state(root)
        _require(run_id in state["runs"], "unknown_run")
        _refresh(state)
        recorded = state["runs"][run_id]
        _require(recorded["status"] == "ready", "stale_run")
        manifest = _read(root / "runs" / f"{run_id}.json")
        _require(recorded.get("manifest_digest") == digest(manifest), "manifest_changed_on_disk")
        # Verify current profile files too: a changed file must not be trusted
        # merely because the registry still records its original revision.
        for dependency in manifest["dependencies"]:
            _profile(root, state, dependency["profile_id"])
        background_values = []
        for dependency in manifest.get("background_dependencies", []):
            background = _background(root, state, dependency["background_id"])
            _require(digest(background) == dependency["digest"], "background_dependency_changed")
            background_values.append(background)
        return {"format": "translation-series-context-v1", "run_id": run_id,
                "source_digest": manifest["source_digest"],
                "book_id": manifest["book_id"], "horizon": manifest["horizon"],
                "rules": deepcopy(manifest["rules"]),
                "full_series_background": background_values,
                "experimental": manifest["experimental"],
                "full_series_context": manifest["full_series_context"],
                "exclude_target_translation_examples": manifest["exclude_target_translation_examples"],
                "example_policy": manifest["example_policy"],
                "manifest_digest": recorded["manifest_digest"]}


def import_cycle(root, cycle_root, profile_id, book_id, horizon):
    """Import private dimensions and their changes without printing their content."""
    cycle_root = private_root(cycle_root)
    first = _read(cycle_root / "dimensions-v1.json")
    second_path = cycle_root / "dimensions-v2.json"
    second = _read(second_path) if second_path.exists() else None
    revisions = []
    versions = [(first["dimensions"], [], first)]
    if second is not None:
        if "dimensions" in second:
            versions.append((second["dimensions"], [], second))
        else:
            versions.append((second.get("base", first)["dimensions"], second["changes"], second))
    for index, (source_rules, changes, artifact) in enumerate(versions, 1):
        rules = deepcopy(source_rules)
        for rule in rules:
            rule["known_at"] = {"book_id": book_id, "paragraph": horizon}
            # Character labels cannot be inferred safely from prose metadata.
            # Until explicitly mapped, constrain those rules to the selected scene.
            if rule["scope"] in {"character", "scene"}:
                rule["original_scope"] = rule["scope"]
                rule["scope"] = "scene"
                rule["end_paragraph"] = horizon
            for change in changes:
                if change["dimension_id"] == rule["id"]:
                    rule["rule"] = change["new_rule"]
        revisions.append(register_profile(root, profile_id, rules,
            {"kind": "dimension_cycle", "cycle_revision": index,
             "artifact_digest": digest(artifact),
             "validation": "single_passage_not_series_validated"}))
    return revisions


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    sub = parser.add_subparsers(dest="operation", required=True)
    p = sub.add_parser("init"); p.add_argument("--series-id", required=True)
    p = sub.add_parser("book")
    for name in ("book-id", "source-digest"): p.add_argument("--" + name, required=True)
    p.add_argument("--publication-order", type=int, required=True)
    p.add_argument("--order-basis", choices=["publication", "corpus_traversal"], default="publication")
    p.add_argument("--paragraph-count", type=int, required=True)
    p = sub.add_parser("profile"); p.add_argument("--profile-id", required=True)
    p.add_argument("--proposal-file", type=Path, required=True)
    p = sub.add_parser("background"); p.add_argument("--background-id", required=True)
    p.add_argument("--book-id", required=True)
    p.add_argument("--content-file", type=Path, required=True)
    p = sub.add_parser("import-cycle")
    p.add_argument("--cycle-root", type=Path, required=True)
    p.add_argument("--profile-id", required=True); p.add_argument("--book-id", required=True)
    p.add_argument("--horizon", type=int, required=True)
    p = sub.add_parser("accept"); p.add_argument("--profile-id", required=True)
    p.add_argument("--user-confirmed", action="store_true")
    p = sub.add_parser("run"); p.add_argument("--run-id", required=True)
    p.add_argument("--book-id", required=True); p.add_argument("--horizon", type=int, required=True)
    p.add_argument("--experimental", action="store_true"); p.add_argument("--character-id", action="append", default=[])
    p = sub.add_parser("status"); p.add_argument("--run-id", required=True)
    args = parser.parse_args()
    try:
        if args.operation == "init": result = initialize(args.root, args.series_id)
        elif args.operation == "book": result = register_book(args.root, args.book_id, args.source_digest, args.publication_order, args.paragraph_count, order_basis=args.order_basis)
        elif args.operation == "profile":
            proposal = _read(args.proposal_file)
            result = register_profile(args.root, args.profile_id, proposal["rules"], proposal.get("provenance", {}))
        elif args.operation == "background":
            result = register_background(args.root, args.background_id, args.book_id, _read(args.content_file))
        elif args.operation == "import-cycle": result = import_cycle(args.root, args.cycle_root, args.profile_id, args.book_id, args.horizon)
        elif args.operation == "accept": result = accept_profile(args.root, args.profile_id, explicit_user_acceptance=args.user_confirmed)
        elif args.operation == "run": result = create_run(args.root, args.run_id, args.book_id, args.horizon, experimental=args.experimental, character_ids=args.character_id)
        else: result = run_status(args.root, args.run_id)
        print(json.dumps({"status": "completed", "operation": args.operation,
                          "revisions": len(result) if isinstance(result, list) else 0,
                          "rules": len(result.get("rules", [])) if isinstance(result, dict) else 0,
                          "run_status": result.get("status") if isinstance(result, dict) else None}))
    except Exception:
        print('{"status":"failed","code":"registry_operation_failed"}')
        raise SystemExit(2)


if __name__ == "__main__":
    main()
