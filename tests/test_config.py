"""Tests for config loading and merging."""

import pytest
from secure_agents.core.config import AppConfig, _deep_merge, load_config, validate_agent_name


def test_deep_merge_basic():
    base = {"a": 1, "b": {"c": 2, "d": 3}}
    override = {"b": {"c": 99}, "e": 5}
    result = _deep_merge(base, override)
    assert result == {"a": 1, "b": {"c": 99, "d": 3}, "e": 5}


def test_deep_merge_override_wins():
    base = {"x": [1, 2, 3]}
    override = {"x": [4, 5]}
    result = _deep_merge(base, override)
    assert result["x"] == [4, 5]


def test_get_agent_config_inherits_defaults():
    config = AppConfig(
        defaults={
            "security": {"max_file_size_mb": 50},
            "storage": {"output_dir": "./output"},
        },
        agents={
            "my_agent": {
                "enabled": True,
                "tools": ["document_parser"],
            },
        },
    )
    merged = config.get_agent_config("my_agent")
    # Agent inherits defaults
    assert merged["security"]["max_file_size_mb"] == 50
    assert merged["storage"]["output_dir"] == "./output"
    # Agent's own values are present
    assert merged["tools"] == ["document_parser"]


def test_get_agent_config_overrides_defaults():
    config = AppConfig(
        defaults={
            "security": {"max_file_size_mb": 50, "allowed_file_types": [".pdf", ".docx"]},
            "storage": {"output_dir": "./output", "retention_days": 90},
        },
        agents={
            "big_file_agent": {
                "enabled": True,
                "tools": ["document_parser"],
                # Override just what this agent needs
                "security": {"max_file_size_mb": 500},
                "storage": {"output_dir": "./output/big_files"},
            },
        },
    )
    merged = config.get_agent_config("big_file_agent")
    # Overridden values
    assert merged["security"]["max_file_size_mb"] == 500
    assert merged["storage"]["output_dir"] == "./output/big_files"
    # Inherited values (not overridden)
    assert merged["security"]["allowed_file_types"] == [".pdf", ".docx"]
    assert merged["storage"]["retention_days"] == 90


def test_get_agent_config_independent():
    """Two agents can have completely different settings."""
    config = AppConfig(
        defaults={"security": {"max_file_size_mb": 50}},
        agents={
            "small_agent": {"enabled": True},
            "large_agent": {"enabled": True, "security": {"max_file_size_mb": 1000}},
        },
    )
    small = config.get_agent_config("small_agent")
    large = config.get_agent_config("large_agent")
    assert small["security"]["max_file_size_mb"] == 50
    assert large["security"]["max_file_size_mb"] == 1000


def test_get_agent_config_missing_returns_defaults():
    config = AppConfig(defaults={"security": {"max_file_size_mb": 50}})
    merged = config.get_agent_config("nonexistent")
    assert merged["security"]["max_file_size_mb"] == 50


def test_cloud_provider_rejected(tmp_path):
    """Cloud providers must be rejected at config load time."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("provider:\n  active: anthropic\n")
    with pytest.raises(ValueError, match="not allowed"):
        load_config(str(config_file))


def test_invalid_agent_name_rejected(tmp_path):
    """Agent names with path traversal or special chars are rejected."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("agents:\n  '../evil': {enabled: true}\n")
    with pytest.raises(ValueError, match="Invalid agent name"):
        load_config(str(config_file))


def test_validate_agent_name():
    assert validate_agent_name("nda_reviewer") is True
    assert validate_agent_name("a") is True
    assert validate_agent_name("agent123") is True
    assert validate_agent_name("") is False
    assert validate_agent_name("../evil") is False
    assert validate_agent_name("Agent") is False  # uppercase
    assert validate_agent_name("123agent") is False  # starts with number
    assert validate_agent_name("a" * 65) is False  # too long
