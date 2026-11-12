"""Generator defaults for the proposed workflow format; no YAML loader.

This is a template for preferences, not runtime configuration or authorization.
Keep field semantics aligned with the workflow proposal and setup reference.
"""

from __future__ import annotations

from copy import deepcopy


_DEFAULT_WORKFLOW = {
    "schema_version": 2,
    "kind": "goals-tmux-workflow",
    "disclosure": {
        "required": True,
        "before": "each_project_skill_use",
        "channel": "user_reply",
        "include": [
            "loaded_yaml_paths",
            "layer_order",
            "absent_layers",
            "scenario",
            "target",
            "cli",
            "model",
            "effort",
            "application_mode",
            "config_digest",
        ],
        "repeat_on_configuration_change": True,
        "on_no_yaml": "explicitly_report_no_yaml_loaded",
        "on_pinned_task": "report_snapshot_and_current_file_difference",
    },
    "execution": {
        "controller": "tmuxctl",
        "viewer": "iterm2",
        "coordinator_role": "delegate_only",
        "deprecated_controllers": ["goals-iterm-skill", "iterm_mcp"],
        "read_project_instructions": True,
        "dispatch_language": "en",
        "report_language": "zh-CN",
        "translate_results_before_deciding": True,
        "preserve_raw_result_codes_and_ids": True,
    },
    "tui": {
        "enabled": True,
        "terminal": "iterm2",
        "mode": "tmux",
        "keep_open": True,
        "allow_input": True,
        "require_visible_proof_before_input": True,
        "show_tui_remediation_limit": 1,
        "on_missing_proof": "stop_and_report",
        "show_after_registry_adoption": True,
    },
    "scenarios": {
        "research": {
            "label": "需求调研",
            "permission_mode": "default",
            "target": None,
            "fallbacks": [],
            "allow_fallback": False,
        },
        "design": {
            "label": "需求设计",
            "permission_mode": "default",
            "target": None,
            "fallbacks": [],
            "allow_fallback": False,
        },
        "test_design": {
            "label": "测试设计",
            "permission_mode": "default",
            "target": None,
            "fallbacks": [],
            "allow_fallback": False,
        },
        "implementation": {
            "label": "需求实现",
            "permission_mode": "default",
            "target": None,
            "fallbacks": [],
            "allow_fallback": False,
        },
        "testing": {
            "label": "需求测试",
            "permission_mode": "default",
            "target": None,
            "fallbacks": [],
            "allow_fallback": False,
        },
        "ui_prototype_html": {
            "label": "UI 原型设计（HTML）",
            "permission_mode": "default",
            "target": None,
            "fallbacks": [],
            "allow_fallback": False,
        },
        "ui_implementation": {
            "label": "UI 实现",
            "permission_mode": "default",
            "target": None,
            "fallbacks": [],
            "allow_fallback": False,
        },
        "ui_automation": {
            "label": "UI 自动化",
            "permission_mode": "default",
            "target": None,
            "fallbacks": [],
            "allow_fallback": False,
        },
        "review": {
            "label": "需求 Review",
            "permission_mode": "default",
            "target": None,
            "fallbacks": [],
            "allow_fallback": False,
        },
        "other": {
            "label": "其他场景",
            "permission_mode": "default",
            "target": None,
            "fallbacks": [],
            "allow_fallback": False,
        },
    },
    "selection": {
        "on_unknown_scenario": "stop_and_report",
        "fallback_order": "listed",
        "fallback_only_after_confirmed_failure": True,
        "fallback_while_auto_retrying": False,
        "fallback_after_uncertain_write": False,
        "fallback_on_unavailable_model_or_flag": False,
        "on_unavailable_model_or_flag": "report_available_and_request_choice",
        "explicit_task_choice_disables_unapproved_fallback": True,
        "fast_mapping": "adapter_capability_only",
    },
    "preflight": {
        "check_runner_capabilities": True,
        "check_cli_version_and_help": True,
        "check_exact_model_and_effort": True,
        "on_unverifiable_capability": "stop_and_report",
        "verify_launch_capture": ["cli", "model", "effort", "permission_mode", "cwd", "prompt_state"],
        "rename_owned_resources": True,
    },
    "dispatch": {
        "protocol": "private_decision_file",
        "require_complete_instruction": True,
        "require_visible_dispatch_id": True,
        "require_capture_before_submit": True,
        "submit_operation": "submit_dispatch",
        "require_input_verified": True,
        "allow_direct_task_paste": False,
    },
    "observation": {
        "inspect_existing_resources_first": True,
        "adopt_matching_active_task": True,
        "wait_method": "inline_watch",
        "max_wait_seconds": 60,
        "create_scheduled_tasks": False,
        "reuse_task_and_monitor": True,
        "while_running_or_retrying": "observe_only",
        "automatic_continue_or_enter": False,
        "automatic_interrupt": False,
        "recover_only_after_confirmed_stop": True,
        "on_uncertain_write": "reconcile_before_retry",
    },
    "concurrency": {
        "counting_unit": "active_cli_instance",
        "scope": "user_local_host",
        "default_max_per_cli": 4,
        "overrides": {},
        "on_limit": "wait_for_slot",
        "on_unknown_liveness": "reserve_slot_and_reconcile",
    },
    "constraints": {
        "tests": "targeted",
        "full_suite": "only_when_necessary",
        "python_reload": False,
        "routine_decisions": "coordinator_within_task_authorization",
        "on_user_decision_required": "preserve_context_and_pause_related_writes",
    },
    "acceptance": {
        "required_evidence": ["pane_capture", "project_artifacts", "targeted_test_results"],
        "per_criterion_evidence": True,
        "cli_claim_is_sufficient": False,
        "idle_prompt_is_sufficient": False,
        "api_receipt_is_sufficient": False,
        "require_monitor_stopped": True,
    },
    "cleanup": {
        "after_acceptance": "close_task_owned_tmux_resources",
        "settle_task": True,
        "explain_unsettled_resources": True,
        "retain_viewer": True,
    },
    "authorization_policy": {
        "bypass_requires_current_task_authorization": True,
        "commit_requires_current_task_authorization": True,
        "push": "deny",
        "publish": "deny",
        "destructive_operations": "deny",
    },
    "hosts": {},
}


def new_workflow() -> dict:
    """Return independent policy defaults with ten unconfigured scenarios."""
    return deepcopy(_DEFAULT_WORKFLOW)
