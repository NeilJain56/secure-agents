"""Load setup_manifest.yaml and resolve which steps are needed for given agents."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from secure_agents.core.config import AppConfig


@dataclass
class CredentialStep:
    key: str
    label: str = ""
    hide_input: bool = False
    condition: str = ""  # e.g. "auth_method == oauth2"
    flow: str = ""  # e.g. "gmail_oauth2" for special flows


@dataclass
class ConfigCheck:
    path: str  # dotted key path into config.yaml
    prompt: str = ""
    sentinel: str = ""  # placeholder value that means "not configured"


@dataclass
class PostInstallAction:
    action: str  # "start_service" or "pull_model"
    service: str = ""
    check_url: str = ""
    model_key: str = ""
    default: str = ""


@dataclass
class SetupPlan:
    """Everything needed to set up the selected agents."""
    agent_names: list[str] = field(default_factory=list)
    provider_name: str = ""
    homebrew_packages: list[str] = field(default_factory=list)
    pip_extras: list[str] = field(default_factory=list)
    post_install: list[PostInstallAction] = field(default_factory=list)
    config_checks: list[ConfigCheck] = field(default_factory=list)
    credentials: list[CredentialStep] = field(default_factory=list)
    directories: list[str] = field(default_factory=list)
    auth_method: str = "app_password"
    email_username: str = ""


def load_manifest(project_root: Path) -> dict[str, Any]:
    """Load setup_manifest.yaml from the project root."""
    manifest_path = project_root / "setup_manifest.yaml"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Setup manifest not found: {manifest_path}")
    with open(manifest_path) as f:
        return yaml.safe_load(f) or {}


def resolve_plan(
    agent_names: list[str],
    config: AppConfig,
    manifest: dict[str, Any],
    provider_override: str | None = None,
) -> SetupPlan:
    """Determine all setup steps needed for the given agents.

    Reads each agent's tools and provider from config, looks up requirements
    in the manifest, deduplicates, and returns an ordered plan.
    """
    plan = SetupPlan(agent_names=agent_names)

    # Collect unique tools and providers across all target agents
    all_tools: set[str] = set()
    providers: set[str] = set()

    for name in agent_names:
        merged = config.get_agent_config(name)
        all_tools.update(merged.get("tools", []))
        prov = merged.get("provider", {}).get("override", config.provider.active)
        providers.add(prov)

    if provider_override:
        providers = {provider_override}

    # There should be exactly one active provider for the setup
    plan.provider_name = provider_override or config.provider.active

    # Resolve email auth method and username from config
    email_cfg = config.defaults.get("email", {}).get("imap", {})
    plan.auth_method = email_cfg.get("auth_method", "app_password")
    plan.email_username = email_cfg.get("username", "")

    # Common requirements
    common = manifest.get("common", {})
    plan.directories.extend(common.get("directories", []))
    plan.pip_extras.extend(common.get("pip_extras", []))

    # Provider requirements
    seen_cred_keys: set[str] = set()
    seen_config_paths: set[str] = set()

    for prov in providers:
        prov_manifest = manifest.get("providers", {}).get(prov, {})
        plan.homebrew_packages.extend(prov_manifest.get("homebrew", []))
        plan.pip_extras.extend(prov_manifest.get("pip_extras", []))

        for action_data in prov_manifest.get("post_install", []):
            plan.post_install.append(PostInstallAction(**action_data))

        for cred in prov_manifest.get("credentials", []):
            if cred["key"] not in seen_cred_keys:
                seen_cred_keys.add(cred["key"])
                plan.credentials.append(CredentialStep(**cred))

    # Tool requirements
    for tool_name in all_tools:
        tool_manifest = manifest.get("tools", {}).get(tool_name, {})

        plan.directories.extend(tool_manifest.get("directories", []))

        for cfg in tool_manifest.get("config_required", []):
            if cfg["path"] not in seen_config_paths:
                seen_config_paths.add(cfg["path"])
                plan.config_checks.append(ConfigCheck(**cfg))

        for cred in tool_manifest.get("credentials", []):
            cred_key = cred.get("key", "")
            # Evaluate condition against current auth_method
            condition = cred.get("condition", "")
            if condition:
                # Parse "auth_method == value"
                if "==" in condition:
                    _, expected = condition.split("==", 1)
                    expected = expected.strip()
                    if plan.auth_method != expected:
                        continue

            if cred_key not in seen_cred_keys:
                seen_cred_keys.add(cred_key)
                plan.credentials.append(CredentialStep(**cred))

    # Deduplicate
    plan.homebrew_packages = list(dict.fromkeys(plan.homebrew_packages))
    plan.pip_extras = list(dict.fromkeys(plan.pip_extras))
    plan.directories = list(dict.fromkeys(plan.directories))

    return plan
