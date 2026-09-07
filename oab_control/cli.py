"""Command-line entry point for local catalog preflight."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from urllib.parse import unquote, urlparse
import uuid

from .catalog import load_catalog, load_reference_manifest, normalized_json
from .environment import load_environment, load_environment_document
from .plan import render_plan
from .renderer import render_openab_values_yaml
from .operations import ControlOperations, OperationError
from .tasks import Task, TaskError, TaskStore, validate_task_catalog_binding
from .worktree import Checkout, WorktreeError, WorktreeManager
from .k8s import EGRESS_MODES, render_k8s_yaml
from .registry import WorkspaceRecord, WorkspaceRegistry
from .backup import BackupError, COMPONENTS, LocalBackup
from .preflight import collect_preflight
from .evidence import EvidenceError, collect as collect_evidence, merge_into as merge_evidence
from .assignments import AssignmentError, AssignmentStore
from .artifacts import ArtifactError, snapshot_task_artifact
from .review_handoff import HandoffError, prepare_code_review, resolve_commit
from .discord_dispatch import DispatchError, dispatch_assignment, reconcile_assignment
from .discord_receipts import ReceiptError, sync_assignment
from .discord_api import DiscordApi
from .discord_preflight import run_preflight
from .runtime_conformance import observe_codex_features, managed_codex_config_digest
from .secrets import SecretError, load_secret_values


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oab-control", description="Validate and normalize an OpenAB agent catalog")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "normalize", "plan", "render-openab", "render-k8s"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("catalog", help="path to the versioned YAML catalog")
        subparser.add_argument("--json", action="store_true", help="emit machine-readable diagnostics")
        subparser.add_argument("--no-path-check", action="store_true", help="skip local path existence checks")
        subparser.add_argument("--check-git", action="store_true", help="require every repository to contain Git metadata")
        subparser.add_argument("--secrets-file", help="non-secret YAML manifest listing allowed Secret references")
        subparser.add_argument("--namespace", default="oab-agents", help="target namespace included in a plan")
        if command == "render-openab":
            subparser.add_argument("--runtime-volume-size", default="10Gi")
        if command == "render-k8s":
            subparser.add_argument(
                "--egress-mode",
                choices=EGRESS_MODES,
                default="proxy-only",
                help="proxy-only denies all egress except the oab-egress proxy; public-tls also allows public HTTPS while still denying private ranges and the metadata address",
            )
    environment = subparsers.add_parser("environment-validate", help="validate the non-secret local bootstrap contract")
    environment.add_argument("environment", help="path to environment.yaml")
    environment.add_argument("--json", action="store_true", help="emit machine-readable diagnostics")
    environment.add_argument("--require-ready", action="store_true", help="reject bootstrap-pending contracts")
    preflight = subparsers.add_parser("preflight", help="read-only deployment readiness check")
    preflight.add_argument("environment", help="ready non-secret environment contract")
    preflight.add_argument("--chart", required=True, help="OpenAB chart directory expected for deployment")
    preflight.add_argument("--json", action="store_true", help="emit machine-readable diagnostics")
    backup = subparsers.add_parser("backup", help="copy declared control state to an attested external target")
    backup.add_argument("sources_file", help="JSON map for catalog/coordination_repository/agent_worktrees_root/k3s_state")
    backup.add_argument("--output", required=True, help="external backup directory")
    backup.add_argument("--attestation", required=True, help="short operator statement that the target is encrypted external storage")
    backup.add_argument("--backup-id")
    backup.add_argument("--yes", action="store_true", help="confirm the backup write")
    backup.add_argument("--json", action="store_true")
    restore = subparsers.add_parser("restore", help="verify and restore a local control-state backup")
    restore.add_argument("backup", help="backup directory containing manifest.json")
    restore.add_argument("destinations_file", help="JSON map of clean destination paths")
    restore.add_argument("--backup-root", help="backup root used for staging (defaults to backup parent)")
    restore.add_argument("--yes", action="store_true", help="confirm the restore write")
    restore.add_argument("--json", action="store_true")
    deploy = subparsers.add_parser("deploy", help="snapshot, show plan, and optionally apply OpenAB Helm values")
    deploy.add_argument("catalog", help="path to the versioned YAML catalog")
    deploy.add_argument("--snapshot-dir", default=".oab-control/snapshots")
    deploy.add_argument("--environment", help="ready non-secret environment contract; required with --yes")
    deploy.add_argument("--chart", help="OpenAB chart directory; required with --yes")
    deploy.add_argument("--release", default="oab-agents")
    deploy.add_argument("--namespace", default="oab-agents")
    deploy.add_argument("--secrets-file")
    deploy.add_argument("--backup-sources", help="optional JSON path map for a pre-deploy external backup; use with both other backup options")
    deploy.add_argument("--backup-output", help="optional external backup target root; use with both other backup options")
    deploy.add_argument("--backup-attestation", help="optional non-secret encryption statement; use with both other backup options")
    deploy.add_argument("--no-path-check", action="store_true")
    deploy.add_argument("--check-git", action="store_true")
    deploy.add_argument("--yes", action="store_true", help="confirm the K3s mutation")
    deploy.add_argument("--json", action="store_true")
    status = subparsers.add_parser("status", help="show local catalog, task, and workspace status")
    status.add_argument("catalog", help="path to the versioned YAML catalog")
    status.add_argument("--namespace", default="oab-agents")
    status.add_argument("--environment", help="optional environment contract used to select the ready deployer kubeconfig")
    status.add_argument("--registry-json")
    status.add_argument("--registry-markdown")
    status.add_argument("--tasks-dir")
    status.add_argument("--secrets-file")
    status.add_argument("--json", action="store_true")
    rollback = subparsers.add_parser("rollback", help="show and optionally apply a catalog snapshot")
    rollback.add_argument("snapshot", help="snapshot directory")
    rollback.add_argument("--catalog", required=True, help="active catalog path")
    rollback.add_argument("--environment", help="ready non-secret environment contract; required with --yes")
    rollback.add_argument("--chart")
    rollback.add_argument("--release", default="oab-agents")
    rollback.add_argument("--namespace", default="oab-agents")
    rollback.add_argument("--yes", action="store_true", help="confirm catalog/K3s mutation")
    rollback.add_argument("--json", action="store_true")
    task_create = subparsers.add_parser("task-create", help="create a leader-owned durable task record")
    task_create.add_argument("task_file", help="JSON file containing a Task shape")
    task_create.add_argument("--catalog", required=True, help="catalog used to bind task routing and repository grants")
    task_create.add_argument("--no-path-check", action="store_true", help="skip local catalog path existence checks")
    task_create.add_argument("--check-git", action="store_true", help="require task repositories to contain Git metadata")
    task_create.add_argument("--secrets-file", help="non-secret YAML manifest listing allowed Secret references")
    task_create.add_argument("--tasks-dir", default=".oab-control/tasks")
    task_create.add_argument("--actor", default="leader")
    task_create.add_argument("--json", action="store_true")
    task_transition = subparsers.add_parser("task-transition", help="advance one task through the explicit lifecycle")
    task_transition.add_argument("task_id")
    task_transition.add_argument("state")
    task_transition.add_argument("--tasks-dir", default=".oab-control/tasks")
    task_transition.add_argument("--evidence-file")
    task_transition.add_argument(
        "--collect",
        metavar="CATALOG",
        help="read commit/branch/repository/owner from the checkout instead of trusting the evidence file",
    )
    task_transition.add_argument("--actor", default="leader")
    task_transition.add_argument("--json", action="store_true")
    task_report = subparsers.add_parser("task-report", help="record a leader-transcribed worker report")
    task_report.add_argument("task_id")
    task_report.add_argument("report_file")
    task_report.add_argument("--tasks-dir", default=".oab-control/tasks")
    task_report.add_argument("--actor", default="leader")
    task_report.add_argument("--json", action="store_true")
    task_review = subparsers.add_parser("task-review", help="record an independent review")
    task_review.add_argument("task_id")
    task_review.add_argument("review_file")
    task_review.add_argument("--tasks-dir", default=".oab-control/tasks")
    task_review.add_argument("--actor", default="leader")
    task_review.add_argument("--json", action="store_true")
    task_collect = subparsers.add_parser(
        "task-collect",
        help="read the verifiable acceptance fields straight from the task's checkout",
    )
    task_collect.add_argument("task_id")
    task_collect.add_argument("--catalog", required=True, help="catalog that binds the task to one grant")
    task_collect.add_argument("--tasks-dir", default=".oab-control/tasks")
    task_collect.add_argument("--json", action="store_true")
    task_list = subparsers.add_parser("task-list", help="list durable task records")
    task_list.add_argument("--tasks-dir", default=".oab-control/tasks")
    task_list.add_argument("--json", action="store_true")
    assignment_prepare = subparsers.add_parser("assignment-prepare", help="prepare a control-plane Work Assignment")
    assignment_prepare.add_argument("task_id")
    assignment_prepare.add_argument("--phase", required=True, choices=("execution", "review"))
    assignment_prepare.add_argument("--agent", required=True)
    assignment_prepare.add_argument("--catalog", required=True)
    assignment_prepare.add_argument("--environment", required=True)
    assignment_prepare.add_argument("--remotes-file", required=True)
    assignment_prepare.add_argument("--tasks-dir", default=".oab-control/tasks")
    assignment_prepare.add_argument("--no-path-check", action="store_true")
    assignment_prepare.add_argument("--json", action="store_true")
    assignment_dispatch = subparsers.add_parser("assignment-dispatch", help="preview and explicitly dispatch a Work Assignment")
    assignment_dispatch.add_argument("assignment_id")
    assignment_dispatch.add_argument("--catalog", required=True)
    assignment_dispatch.add_argument("--tasks-dir", default=".oab-control/tasks")
    assignment_dispatch.add_argument("--secrets-file", required=True)
    assignment_dispatch.add_argument("--yes", action="store_true")
    assignment_dispatch.add_argument("--json", action="store_true")
    assignment_sync = subparsers.add_parser("assignment-sync", help="pull the target bot response from an assignment thread")
    assignment_sync.add_argument("assignment_id")
    assignment_sync.add_argument("--catalog", required=True)
    assignment_sync.add_argument("--tasks-dir", default=".oab-control/tasks")
    assignment_sync.add_argument("--secrets-file", required=True)
    assignment_sync.add_argument("--json", action="store_true")
    assignment_reconcile = subparsers.add_parser("assignment-reconcile", help="resolve an unknown Discord dispatch by nonce")
    assignment_reconcile.add_argument("assignment_id")
    assignment_reconcile.add_argument("--catalog", required=True)
    assignment_reconcile.add_argument("--tasks-dir", default=".oab-control/tasks")
    assignment_reconcile.add_argument("--secrets-file", required=True)
    assignment_reconcile.add_argument("--json", action="store_true")
    assignment_prune = subparsers.add_parser("assignment-prune", help="explicitly prune closed assignment artifacts")
    assignment_prune.add_argument("assignment_id")
    assignment_prune.add_argument("--tasks-dir", default=".oab-control/tasks")
    assignment_prune.add_argument("--environment", required=True)
    assignment_prune.add_argument("--remotes-file")
    assignment_prune.add_argument("--yes", action="store_true")
    assignment_prune.add_argument("--json", action="store_true")
    discord_preflight = subparsers.add_parser("discord-preflight", help="online Discord channel capability preflight")
    discord_preflight.add_argument("--catalog", required=True)
    discord_preflight.add_argument("--secrets-file", required=True)
    discord_preflight.add_argument("--json", action="store_true")
    runtime = subparsers.add_parser("runtime-conformance", help="verify a leader Codex feature observation")
    runtime.add_argument("--features-file", required=True, help="captured output of codex features list")
    runtime.add_argument("--image-digest", required=True)
    runtime.add_argument("--config-digest", default=managed_codex_config_digest())
    runtime.add_argument("--json", action="store_true")
    materialize = subparsers.add_parser("worktree-materialize", help="materialize independent Git checkouts for one agent")
    materialize.add_argument("catalog", help="path to the versioned YAML catalog")
    materialize.add_argument("agent_id")
    materialize.add_argument("task_id")
    materialize.add_argument("--remotes-file", required=True, help="JSON map of exact repository path to delivery remote")
    materialize.add_argument(
        "--allow-local-remotes",
        action="store_true",
        help="accept file:// delivery remotes; for local bootstrap before a GitLab remote exists",
    )
    materialize.add_argument("--branch")
    materialize.add_argument("--no-path-check", action="store_true")
    materialize.add_argument("--check-git", action="store_true")
    materialize.add_argument("--secrets-file")
    materialize.add_argument("--registry-json", help="persist the one-row-per-agent workspace registry")
    materialize.add_argument("--registry-markdown", help="Markdown projection path for the workspace registry")
    materialize.add_argument("--tasks-dir", default=".oab-control/tasks", help="task store used to verify the task envelope")
    materialize.add_argument("--json", action="store_true")
    cleanup = subparsers.add_parser("worktree-cleanup", help="clean a closed task branch while retaining the agent worktree")
    cleanup.add_argument("catalog", help="path to the versioned YAML catalog")
    cleanup.add_argument("agent_id")
    cleanup.add_argument("task_id")
    cleanup.add_argument("--remotes-file", required=True, help="JSON map of exact repository path to delivery remote")
    cleanup.add_argument(
        "--allow-local-remotes",
        action="store_true",
        help="accept file:// delivery remotes; for local bootstrap before a GitLab remote exists",
    )
    cleanup.add_argument("--tasks-dir", default=".oab-control/tasks")
    cleanup.add_argument("--registry-json")
    cleanup.add_argument("--registry-markdown")
    cleanup.add_argument("--yes", action="store_true")
    cleanup.add_argument("--no-path-check", action="store_true")
    cleanup.add_argument("--check-git", action="store_true")
    cleanup.add_argument("--json", action="store_true")
    retire = subparsers.add_parser("worktree-retire", help="remove an agent worktree after explicit confirmation")
    retire.add_argument("--catalog", required=True, help="catalog used to verify the agent-owned worktree path")
    retire.add_argument("agent_id")
    retire.add_argument("worktree_path")
    retire.add_argument("--registry-json", help="mark the workspace retired in the registry")
    retire.add_argument("--registry-markdown", help="Markdown projection path for the workspace registry")
    retire.add_argument("--yes", action="store_true")
    retire.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "environment-validate":
        diagnostics = load_environment(args.environment, require_ready=args.require_ready)
        if args.json:
            sys.stdout.write(json.dumps({"valid": not diagnostics, "diagnostics": [diagnostic.as_dict() for diagnostic in diagnostics]}, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        elif diagnostics:
            for diagnostic in diagnostics:
                sys.stderr.write(f"{diagnostic.path}: {diagnostic.code}: {diagnostic.message}\n")
        else:
            sys.stdout.write("environment contract valid\n")
        return 1 if diagnostics else 0
    if args.command == "preflight":
        result = collect_preflight(args.environment, chart_path=args.chart)
        sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        return 0 if result["ready"] else 1
    if args.command == "discord-preflight":
        try:
            catalog = _require_catalog(args.catalog)
            agents = catalog["agents"]
            clients = {}
            values = load_secret_values(args.secrets_file)
            for agent_id, agent in agents.items():
                if not isinstance(agent, dict):
                    raise AssignmentError("invalid agent catalog entry")
                reference = agent["discord"]["bot_secret_ref"]
                if reference not in values:
                    raise SecretError(f"unresolved Discord Secret reference: {reference}")
                clients[agent_id] = DiscordApi(values[reference])
            result = run_preflight(catalog, clients)
        except (AssignmentError, SecretError, OSError, ValueError) as exc:
            result = {"ready": False, "errors": [str(exc)]}
        sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        return 0 if result["ready"] else 1
    if args.command == "runtime-conformance":
        try:
            output = Path(args.features_file).read_text(encoding="utf-8")
            result = observe_codex_features(output, image_digest=args.image_digest, config_digest=args.config_digest)
        except OSError as exc:
            result = {"leader_subagents": "non_conformant", "error": type(exc).__name__}
        sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        return 0 if result.get("leader_subagents") == "conformant" else 1
    if args.command in {"backup", "restore"}:
        try:
            if args.command == "backup":
                sources = _path_manifest(_json_file(args.sources_file), "backup sources")
                if not args.yes:
                    result = {"confirmed": False, "applied": False, "reason": "human confirmation required; backup target was not modified", "output": str(Path(args.output).resolve(strict=False))}
                else:
                    result = LocalBackup(args.output).create(sources, encryption_attestation=args.attestation, backup_id=args.backup_id).as_dict()
                    result["confirmed"] = True
            else:
                destinations = _path_manifest(_json_file(args.destinations_file), "restore destinations")
                backup_root = args.backup_root or str(Path(args.backup).resolve(strict=True).parent)
                result = LocalBackup(backup_root).restore(args.backup, destinations, confirmed=args.yes)
                result["confirmed"] = True
        except (BackupError, OSError, ValueError, TypeError, KeyError) as exc:
            if args.json:
                sys.stdout.write(json.dumps({"valid": False, "error": str(exc)}, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            else:
                sys.stderr.write(f"operation failed: {exc}\n")
            return 1
        sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n")
        return 0
    if args.command.startswith("task-") or args.command.startswith("worktree-") or args.command.startswith("assignment-"):
        try:
            if args.command == "assignment-prepare":
                catalog, diagnostics = load_catalog(args.catalog, check_paths=not args.no_path_check)
                if diagnostics or catalog is None:
                    raise AssignmentError("catalog validation failed: " + "; ".join(f"{item.path}: {item.code}" for item in diagnostics))
                environment, environment_errors = load_environment_document(args.environment)
                if environment_errors or not isinstance(environment, dict):
                    raise AssignmentError("unable to load environment contract")
                environment_diagnostics = load_environment(args.environment, require_ready=True)
                if environment_diagnostics:
                    raise AssignmentError("environment contract is not ready")
                inbox_root = environment["paths"]["assignment_inbox_root"]
                subject = None
                if args.phase == "review":
                    task_record = TaskStore(args.tasks_dir).get(args.task_id)
                    target = catalog["agents"].get(args.agent)
                    if not isinstance(target, dict):
                        raise AssignmentError("unknown assignment target")
                    if task_record.kind == "code":
                        remote_document = _json_file(args.remotes_file)
                        if not isinstance(remote_document, dict) or not isinstance(remote_document.get(task_record.repository), str):
                            raise AssignmentError("review handoff has no delivery mirror for the task repository")
                        mirror = _local_file_remote(remote_document[task_record.repository])
                        source_checkout = Path(task_record.worktree_path) / task_record.checkout_subpath
                        reviewer_checkout = Path(target["worktree"]["path"]) / task_record.checkout_subpath
                        subject = prepare_code_review(
                            source_checkout=source_checkout, reviewer_checkout=reviewer_checkout, mirror=mirror,
                            task_id=task_record.task_id, expected_branch=task_record.branch,
                            base_sha=resolve_commit(source_checkout, task_record.base_branch), base_ref=task_record.base_branch,
                        )
                        subject["repository"] = task_record.repository
                    else:
                        subject = snapshot_task_artifact(args.tasks_dir, task_record.task_id)
                result = AssignmentStore(args.tasks_dir).prepare(
                    task_id=args.task_id, phase=args.phase, target_agent_id=args.agent,
                    catalog=catalog, inbox_root=inbox_root, subject=subject,
                ).as_dict()
            elif args.command == "assignment-dispatch":
                catalog = _require_catalog(args.catalog)
                clients = _discord_clients(catalog, args.secrets_file)
                preflight = run_preflight(catalog, clients)
                if not preflight.get("ready"):
                    raise DispatchError("Discord preflight failed: " + "; ".join(preflight.get("errors", [])))
                leader_id = _leader_agent_id(catalog)
                api = clients[leader_id]
                nonce = uuid.uuid4().hex
                preview = dispatch_assignment(store=AssignmentStore(args.tasks_dir), assignment_id=args.assignment_id, catalog=catalog, confirmed=False, send=api.send, nonce_factory=lambda: nonce)
                if not args.yes:
                    result = preview
                else:
                    sys.stderr.write(json.dumps({"preview": preview}, ensure_ascii=False, sort_keys=True) + "\n")
                    result = dispatch_assignment(store=AssignmentStore(args.tasks_dir), assignment_id=args.assignment_id, catalog=catalog, confirmed=True, send=api.send, nonce_factory=lambda: nonce)
            elif args.command == "assignment-sync":
                catalog = _require_catalog(args.catalog)
                clients = _discord_clients(catalog, args.secrets_file)
                preflight = run_preflight(catalog, clients)
                if not preflight.get("ready"):
                    raise ReceiptError("Discord preflight failed: " + "; ".join(preflight.get("errors", [])))
                api = clients[_leader_agent_id(catalog)]
                result = sync_assignment(
                    store=AssignmentStore(args.tasks_dir), assignment_id=args.assignment_id, catalog=catalog,
                    discover_thread=api.discover_thread, read_messages=api.read_messages,
                )
            elif args.command == "assignment-reconcile":
                catalog = _require_catalog(args.catalog)
                clients = _discord_clients(catalog, args.secrets_file)
                preflight = run_preflight(catalog, clients)
                if not preflight.get("ready"):
                    raise DispatchError("Discord preflight failed: " + "; ".join(preflight.get("errors", [])))
                api = clients[_leader_agent_id(catalog)]
                result = reconcile_assignment(
                    store=AssignmentStore(args.tasks_dir), assignment_id=args.assignment_id,
                    catalog=catalog, find_messages=api.find_messages,
                )
            elif args.command == "assignment-prune":
                environment, environment_errors = load_environment_document(args.environment)
                if environment_errors or not isinstance(environment, dict) or load_environment(args.environment, require_ready=True):
                    raise AssignmentError("environment contract is not ready")
                store = AssignmentStore(args.tasks_dir)
                assignment = store.find(args.assignment_id)
                mirror = None
                if assignment.subject.get("review_ref"):
                    if not args.remotes_file:
                        raise AssignmentError("code review prune requires --remotes-file")
                    task = TaskStore(args.tasks_dir).get(assignment.task_id)
                    remotes = _json_file(args.remotes_file)
                    if not isinstance(remotes, dict) or not isinstance(remotes.get(task.repository), str):
                        raise AssignmentError("review prune has no delivery mirror for the task repository")
                    mirror = _local_file_remote(remotes[task.repository])
                result = store.prune(assignment, inbox_root=environment["paths"]["assignment_inbox_root"], mirror=mirror, confirmed=args.yes)
            elif args.command == "task-create":
                task = _task_from_file(args.task_file)
                secret_refs = identity_refs = None
                reference_diagnostics = []
                if args.secrets_file:
                    secret_refs, identity_refs, reference_diagnostics = load_reference_manifest(args.secrets_file)
                catalog, diagnostics = load_catalog(
                    args.catalog,
                    check_paths=not args.no_path_check,
                    check_git=args.check_git,
                    available_secret_refs=secret_refs,
                    available_identity_refs=identity_refs,
                )
                diagnostics = reference_diagnostics + diagnostics
                if diagnostics or catalog is None:
                    raise TaskError("catalog validation failed: " + "; ".join(f"{item.path}: {item.code}" for item in diagnostics))
                validate_task_catalog_binding(task, catalog)
                result = TaskStore(args.tasks_dir).create(task, actor=args.actor).as_dict()
            elif args.command == "task-transition":
                evidence = _json_file(args.evidence_file) if args.evidence_file else {}
                verified: tuple[str, ...] = ()
                collected_catalog = None
                if args.collect:
                    if not isinstance(evidence, dict):
                        raise TaskError("evidence file must be a JSON object when collecting")
                    store = TaskStore(args.tasks_dir)
                    collected_catalog = _require_catalog(args.collect)
                    collected = collect_evidence(store.get(args.task_id), collected_catalog, tasks_root=args.tasks_dir)
                    evidence = merge_evidence(evidence, collected)
                    verified = tuple(collected.fields)
                if args.state == "accepted" and not args.collect:
                    raise TaskError("acceptance requires --collect with the current catalog")
                result = TaskStore(args.tasks_dir).transition(
                    args.task_id, args.state, actor=args.actor, evidence=evidence, verified_fields=verified,
                    catalog=collected_catalog, require_verified=args.state == "accepted",
                ).as_dict()
            elif args.command == "task-report":
                result = TaskStore(args.tasks_dir).write_report(args.task_id, _text_file(args.report_file), actor=args.actor).as_dict()
            elif args.command == "task-review":
                result = TaskStore(args.tasks_dir).write_review(args.task_id, _text_file(args.review_file), actor=args.actor).as_dict()
            elif args.command == "task-collect":
                task = TaskStore(args.tasks_dir).get(args.task_id)
                result = collect_evidence(task, _require_catalog(args.catalog), tasks_root=args.tasks_dir).as_dict()
            elif args.command == "task-list":
                result = {"tasks": [task.as_dict() for task in TaskStore(args.tasks_dir).list()]}
            elif args.command == "worktree-materialize":
                secret_refs = identity_refs = None
                reference_diagnostics = []
                if args.secrets_file:
                    secret_refs, identity_refs, reference_diagnostics = load_reference_manifest(args.secrets_file)
                catalog, diagnostics = load_catalog(
                    args.catalog,
                    check_paths=not args.no_path_check,
                    check_git=args.check_git,
                    available_secret_refs=secret_refs,
                    available_identity_refs=identity_refs,
                )
                diagnostics = reference_diagnostics + diagnostics
                if diagnostics or catalog is None:
                    raise WorktreeError("catalog validation failed: " + "; ".join(f"{item.path}: {item.code}" for item in diagnostics))
                remotes = _json_file(args.remotes_file)
                if not isinstance(remotes, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in remotes.items()):
                    raise WorktreeError("remote manifest must be a JSON object of repository path to remote")
                task = None
                if args.tasks_dir:
                    task = TaskStore(args.tasks_dir).get(args.task_id)
                    if task.agent_id != args.agent_id:
                        raise WorktreeError("task envelope belongs to a different agent")
                    if task.state not in {"assigned", "active", "needs-reconciliation"}:
                        raise WorktreeError("worktree materialization requires an assigned/active/reconciliation task")
                    agent_definition = catalog["agents"].get(args.agent_id)
                    if not isinstance(agent_definition, dict) or task.worktree_path != agent_definition["worktree"]["path"]:
                        raise WorktreeError("task envelope worktree does not match the catalog")
                    if not any(
                        task.repository == grant["repository"] and task.checkout_subpath == grant["checkout_subpath"]
                        for grant in agent_definition["repository_grants"]
                    ):
                        raise WorktreeError("task envelope repository grant does not match the catalog")
                    try:
                        validate_task_catalog_binding(task, catalog)
                    except TaskError as exc:
                        raise WorktreeError(str(exc)) from exc
                    if args.branch and args.branch != task.branch:
                        raise WorktreeError("requested branch differs from the task envelope")
                checkouts = WorktreeManager(remotes=remotes, allow_local_remotes=args.allow_local_remotes).materialize(
                    agent_id=args.agent_id,
                    agent=catalog["agents"][args.agent_id],
                    task_id=args.task_id,
                    branch=args.branch or (task.branch if task is not None else None),
                )
                if args.registry_json:
                    registry_path = args.registry_markdown or str(args.registry_json) + ".md"
                    first = checkouts[0] if checkouts else None
                    if first is None:
                        raise WorktreeError("agent has no repository grants")
                    project_scope = ", ".join(checkout.repository for checkout in checkouts)
                    WorkspaceRegistry(args.registry_json, registry_path).upsert(
                        WorkspaceRecord(
                            workspace_id=f"workspace-{args.agent_id}",
                            owner_agent=args.agent_id,
                            project_scope=project_scope,
                            worktree_path=catalog["agents"][args.agent_id]["worktree"]["path"],
                            default_branch=first.base_branch,
                            current_task_id=args.task_id,
                            current_branch=first.branch,
                            status="active",
                        )
                    )
                result = {"checkouts": [checkout.__dict__ | {"read_only": checkout.read_only} for checkout in checkouts]}
            elif args.command == "worktree-cleanup":
                catalog, diagnostics = load_catalog(args.catalog, check_paths=not args.no_path_check, check_git=args.check_git)
                if diagnostics or catalog is None:
                    raise WorktreeError("catalog validation failed: " + "; ".join(f"{item.path}: {item.code}" for item in diagnostics))
                task = TaskStore(args.tasks_dir).get(args.task_id)
                if task.agent_id != args.agent_id or task.state != "closed":
                    raise WorktreeError("task cleanup requires the agent's closed task")
                if task.kind == "code" and not task.merge_completed and task.closure_kind != "cancelled":
                    raise WorktreeError("code task cleanup requires persisted merge completion or cancellation evidence")
                agent = catalog["agents"].get(args.agent_id)
                if not isinstance(agent, dict):
                    raise WorktreeError("agent is not present in catalog")
                if not any(
                    task.repository == grant["repository"] and task.checkout_subpath == grant["checkout_subpath"]
                    for grant in agent["repository_grants"]
                ):
                    raise WorktreeError("task envelope repository grant does not match the catalog")
                matching_grant = next(
                    grant for grant in agent["repository_grants"]
                    if task.repository == grant["repository"] and task.checkout_subpath == grant["checkout_subpath"]
                )
                if task.base_branch != matching_grant["base_branch"]:
                    raise WorktreeError("task base branch does not match the catalog grant")
                remotes = _json_file(args.remotes_file)
                if not isinstance(remotes, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in remotes.items()):
                    raise WorktreeError("remote manifest must be a JSON object of repository path to remote")
                worktree_root = Path(agent["worktree"]["path"]).resolve(strict=False)
                checkouts = []
                for grant in agent["repository_grants"]:
                    checkout_path = (worktree_root / grant["checkout_subpath"]).resolve(strict=False)
                    if checkout_path == worktree_root or not checkout_path.is_relative_to(worktree_root) or not checkout_path.is_dir():
                        raise WorktreeError("task checkout is missing or escapes the agent worktree")
                    repository = str(Path(grant["repository"]).resolve(strict=False))
                    remote = remotes.get(repository)
                    if not remote:
                        raise WorktreeError(f"missing configured GitLab remote for repository: {repository}")
                    checkouts.append(
                        Checkout(
                            agent_id=args.agent_id,
                            repository=repository,
                            path=str(checkout_path),
                            container_mount_path=f"{agent['worktree']['container_mount_path']}/{grant['checkout_subpath']}",
                            access=grant["access"],
                            branch=task.branch,
                            base_branch=grant["base_branch"],
                            origin=remote,
                            created=False,
                        )
                    )
                WorktreeManager(remotes=remotes, allow_local_remotes=args.allow_local_remotes).cleanup_task(checkouts=checkouts, confirmed=args.yes)
                if args.registry_json:
                    registry_path = args.registry_markdown or str(args.registry_json) + ".md"
                    WorkspaceRegistry(args.registry_json, registry_path).clear_task(f"workspace-{args.agent_id}")
                result = {"agent_id": args.agent_id, "task_id": args.task_id, "cleaned": args.yes, "worktree_retained": True}
            else:
                catalog, diagnostics = load_catalog(args.catalog, check_paths=False)
                if diagnostics or catalog is None:
                    raise WorktreeError("catalog validation failed: " + "; ".join(f"{item.path}: {item.code}" for item in diagnostics))
                try:
                    expected_path = catalog["agents"][args.agent_id]["worktree"]["path"]
                except KeyError as exc:
                    raise WorktreeError("agent is not present in catalog") from exc
                if str(expected_path) != str(args.worktree_path):
                    raise WorktreeError("retirement path does not match the catalog-owned worktree")
                registry = None
                if args.registry_json:
                    registry_path = args.registry_markdown or str(args.registry_json) + ".md"
                    registry = WorkspaceRegistry(args.registry_json, registry_path)
                    workspace_id = f"workspace-{args.agent_id}"
                    if not any(record.workspace_id == workspace_id for record in registry.load()):
                        raise WorktreeError("workspace registry has no row for the agent; refusing partial retirement")
                WorktreeManager(remotes={}).retire_agent(agent_id=args.agent_id, worktree_path=args.worktree_path, confirmed=args.yes)
                if registry is not None:
                    registry.retire(
                        workspace_id,
                        record="retired by explicit operator confirmation",
                    )
                result = {"agent_id": args.agent_id, "retired": args.yes}
        except (OSError, KeyError, TypeError, ValueError, TaskError, WorktreeError, EvidenceError, AssignmentError, ArtifactError, HandoffError, DispatchError, ReceiptError, SecretError) as exc:
            if args.json:
                sys.stdout.write(json.dumps({"valid": False, "error": str(exc)}, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            else:
                sys.stderr.write(f"operation failed: {exc}\n")
            return 1
        sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n")
        return 0
    if args.command in {"deploy", "status", "rollback"}:
        try:
            operations = ControlOperations(namespace=args.namespace)
            if args.command == "deploy":
                backup_sources = None
                if args.backup_sources:
                    backup_sources = _path_manifest(_json_file(args.backup_sources), "backup sources")
                result = operations.deploy(
                    catalog_path=args.catalog,
                    snapshot_dir=args.snapshot_dir,
                    environment_file=args.environment,
                    secrets_file=args.secrets_file,
                    chart_path=args.chart,
                    release=args.release,
                    confirmed=args.yes,
                    check_paths=not args.no_path_check,
                    check_git=args.check_git,
                    backup_sources=backup_sources,
                    backup_output=args.backup_output,
                    backup_attestation=args.backup_attestation,
                )
            elif args.command == "status":
                result = operations.status(
                    catalog_path=args.catalog,
                    environment_file=args.environment,
                    registry_json=args.registry_json,
                    registry_markdown=args.registry_markdown,
                    tasks_dir=args.tasks_dir,
                    secrets_file=args.secrets_file,
                )
            else:
                result = operations.rollback(
                    snapshot_path=args.snapshot,
                    catalog_path=args.catalog,
                    environment_file=args.environment,
                    chart_path=args.chart,
                    release=args.release,
                    confirmed=args.yes,
                )
        except (OperationError, OSError, ValueError, TypeError, KeyError) as exc:
            if args.json:
                sys.stdout.write(json.dumps({"valid": False, "error": str(exc)}, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            else:
                sys.stderr.write(f"operation failed: {exc}\n")
            return 1
        if args.json or args.command in {"deploy", "rollback"}:
            sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        else:
            sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        return 0
    reference_diagnostics = []
    secret_refs = identity_refs = None
    if args.secrets_file:
        secret_refs, identity_refs, reference_diagnostics = load_reference_manifest(args.secrets_file)
    catalog, diagnostics = load_catalog(
        args.catalog,
        check_paths=not args.no_path_check,
        check_git=args.check_git,
        available_secret_refs=secret_refs,
        available_identity_refs=identity_refs,
    )
    diagnostics = reference_diagnostics + diagnostics
    if args.command == "normalize" and not diagnostics and catalog is not None:
        sys.stdout.write(normalized_json(catalog))
        return 0
    if args.command == "plan" and not diagnostics and catalog is not None:
        sys.stdout.write(json.dumps(render_plan(catalog, namespace=args.namespace), ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        return 0
    if args.command == "render-openab" and not diagnostics and catalog is not None:
        sys.stdout.write(render_openab_values_yaml(catalog, runtime_volume_size=args.runtime_volume_size))
        return 0
    if args.command == "render-k8s" and not diagnostics and catalog is not None:
        sys.stdout.write(render_k8s_yaml(catalog, namespace=args.namespace, egress_mode=args.egress_mode))
        return 0
    if args.json:
        payload: dict[str, object] = {
            "valid": not diagnostics,
            "diagnostics": [diagnostic.as_dict() for diagnostic in diagnostics],
        }
        if catalog is not None and not diagnostics:
            payload["catalog"] = catalog
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    elif diagnostics:
        for diagnostic in diagnostics:
            sys.stderr.write(f"{diagnostic.path}: {diagnostic.code}: {diagnostic.message}\n")
    else:
        sys.stdout.write("catalog valid\n")
    return 1 if diagnostics else 0


def _require_catalog(path: str) -> dict[str, object]:
    """Load a catalog for evidence collection, refusing an invalid one."""

    catalog, diagnostics = load_catalog(path, check_paths=False)
    if diagnostics or catalog is None:
        raise TaskError("catalog validation failed: " + "; ".join(f"{item.path}: {item.code}" for item in diagnostics))
    return catalog


def _json_file(path: str) -> object:
    try:
        with open(path, "r", encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to read JSON input: {type(exc).__name__}") from exc


def _path_manifest(document: object, label: str) -> dict[str, str]:
    if not isinstance(document, dict) or set(document) != set(COMPONENTS) or not all(isinstance(key, str) and isinstance(value, str) for key, value in document.items()):
        raise ValueError(f"{label} must be a JSON object with exactly: {', '.join(COMPONENTS)}")
    return {str(key): str(value) for key, value in document.items()}


def _text_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as stream:
            return stream.read()
    except OSError as exc:
        raise ValueError(f"unable to read text input: {type(exc).__name__}") from exc


def _local_file_remote(value: str) -> Path:
    parsed = urlparse(value)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise AssignmentError("version 1 review handoff requires a host-local file:// mirror")
    path = Path(unquote(parsed.path)).resolve(strict=True)
    if not path.is_dir():
        raise AssignmentError("delivery mirror does not exist")
    return path


def _leader_token(catalog: dict[str, object], secrets_file: str) -> str:
    agents = catalog.get("agents")
    leaders = [agent for agent in agents.values() if isinstance(agent, dict) and agent.get("role") == "leader"] if isinstance(agents, dict) else []
    if len(leaders) != 1:
        raise AssignmentError("catalog must contain exactly one leader")
    reference = leaders[0]["discord"]["bot_secret_ref"]
    values = load_secret_values(secrets_file)
    if reference not in values:
        raise SecretError("leader Discord Secret reference is unresolved")
    return values[reference]


def _leader_agent_id(catalog: dict[str, object]) -> str:
    agents = catalog.get("agents")
    leaders = [(agent_id, agent) for agent_id, agent in agents.items() if isinstance(agent, dict) and agent.get("role") == "leader"] if isinstance(agents, dict) else []
    if len(leaders) != 1:
        raise AssignmentError("catalog must contain exactly one leader")
    return str(leaders[0][0])


def _discord_clients(catalog: dict[str, object], secrets_file: str) -> dict[str, DiscordApi]:
    values = load_secret_values(secrets_file)
    agents = catalog.get("agents")
    if not isinstance(agents, dict):
        raise AssignmentError("catalog has no agents")
    clients: dict[str, DiscordApi] = {}
    for agent_id, agent in agents.items():
        if not isinstance(agent, dict) or not isinstance(agent.get("discord"), dict):
            raise AssignmentError("invalid Discord catalog binding")
        reference = agent["discord"].get("bot_secret_ref")
        if not isinstance(reference, str) or reference not in values:
            raise SecretError(f"unresolved Discord Secret reference: {reference}")
        clients[str(agent_id)] = DiscordApi(values[reference])
    return clients


def _task_from_file(path: str) -> Task:
    document = _json_file(path)
    if not isinstance(document, dict):
        raise ValueError("task input must be a JSON object")
    document["canonical_sources"] = tuple(document.get("canonical_sources", []))
    document["tests"] = tuple(document.get("tests", []))
    return Task(**document)


if __name__ == "__main__":
    raise SystemExit(main())
