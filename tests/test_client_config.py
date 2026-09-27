"""Tests for client config loading and mode-flag validation."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.llm_proxy.client_config import load_client_config


CLEARSLOPE_PATH = Path("config/clients/clearslope.json")


def _write_tmp_config(tmp_path: Path, overrides: dict) -> Path:
    raw = json.loads(CLEARSLOPE_PATH.read_text(encoding="utf-8"))
    raw.update(overrides)
    out = tmp_path / "tmp_client.json"
    out.write_text(json.dumps(raw), encoding="utf-8")
    return out


def test_pipa_mode_defaults_false_for_legacy_configs(tmp_path: Path) -> None:
    cfg_path = _write_tmp_config(tmp_path, {"gdpr_mode": False})
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    raw.pop("pipa_mode", None)
    cfg_path.write_text(json.dumps(raw), encoding="utf-8")
    cfg = load_client_config(cfg_path)
    assert cfg.pipa_mode is False


def test_pipa_mode_loads_when_set(tmp_path: Path) -> None:
    cfg_path = _write_tmp_config(tmp_path, {"pipa_mode": True, "gdpr_mode": False})
    cfg = load_client_config(cfg_path)
    assert cfg.pipa_mode is True
    assert cfg.gdpr_mode is False


def test_both_modes_set_raises(tmp_path: Path) -> None:
    cfg_path = _write_tmp_config(tmp_path, {"pipa_mode": True, "gdpr_mode": True})
    with pytest.raises(ValueError, match="mutually exclusive"):
        load_client_config(cfg_path)


def test_clearslope_config_has_pipa_mode_enabled() -> None:
    """Production config gate: clearslope.json must be running PIPA mode."""
    cfg = load_client_config(CLEARSLOPE_PATH)
    assert cfg.pipa_mode is True, "clearslope.json must opt into pipa_mode for BC"
    assert cfg.gdpr_mode is False
