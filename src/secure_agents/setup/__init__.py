"""Agent-aware setup system.

Reads config.yaml + setup_manifest.yaml to determine what each agent needs,
then runs idempotent setup steps in dependency order.
"""
