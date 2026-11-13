"""Tests for the ClicheFactory CLI argument parsing, config, and command routing."""
from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from clichefactory._config import CLIConfig, LocalConfig, ServiceConfig, load_config, save_config
from clichefactory.cli import build_parser, cmd_doctor, main


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

class TestArgParsing:
    def test_no_command_shows_help(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main([])
        assert exc.value.code == 0

    def test_version_flag(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--version"])
        assert exc.value.code == 0
        assert "clichefactory" in capsys.readouterr().out

    def test_extract_requires_schema(self):
        with pytest.raises(SystemExit):
            main(["extract", "file.pdf"])

    def test_extract_parses_all_flags(self):
        parser = build_parser()
        args = parser.parse_args([
            "extract", "invoice.pdf",
            "--schema", "schema.json",
            "--extraction-mode", "fast",
            "--mode", "local",
            "--model", "openai/gpt-4o",
            "--model-api-key", "sk-xxx",
            "--ocr-engine", "rapidocr",
            "--lang", "deu+eng",
            "--output", "result.json",
        ])
        assert args.command == "extract"
        assert args.file == "invoice.pdf"
        assert args.schema == "schema.json"
        assert args.extraction_mode == "fast"
        assert args.client_mode == "local"
        assert args.model == "openai/gpt-4o"
        assert args.model_api_key == "sk-xxx"
        assert args.ocr_engine == "rapidocr"
        assert args.lang == "deu+eng"
        assert args.output == "result.json"

    def test_extract_batch_accepts_multiple_files(self):
        parser = build_parser()
        args = parser.parse_args([
            "extract-batch", "a.pdf", "b.pdf", "c.pdf",
            "--schema", "schema.json",
            "--max-concurrency", "10",
        ])
        assert args.command == "extract-batch"
        assert args.files == ["a.pdf", "b.pdf", "c.pdf"]
        assert args.max_concurrency == 10

    def test_to_markdown_parses_flags(self):
        parser = build_parser()
        args = parser.parse_args([
            "to-markdown", "doc.pdf",
            "--conversion-mode", "fast",
            "--ocr-engine", "tesseract",
            "--lang", "deu+eng",
            "--output", "out.md",
        ])
        assert args.command == "to-markdown"
        assert args.file == "doc.pdf"
        assert args.conversion_mode == "fast"
        assert args.ocr_engine == "tesseract"
        assert args.lang == "deu+eng"
        assert args.output == "out.md"

    def test_to_markdown_batch_accepts_files(self):
        parser = build_parser()
        args = parser.parse_args([
            "to-markdown-batch", "a.pdf", "b.pdf",
            "--max-concurrency", "3",
        ])
        assert args.command == "to-markdown-batch"
        assert args.files == ["a.pdf", "b.pdf"]
        assert args.max_concurrency == 3

    def test_configure_local_flag(self):
        parser = build_parser()
        args = parser.parse_args(["configure", "--local"])
        assert args.command == "configure"
        assert args.local is True

    def test_doctor_command(self):
        parser = build_parser()
        args = parser.parse_args(["doctor"])
        assert args.command == "doctor"

    def test_invalid_extraction_mode_rejected(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["extract", "f.pdf", "--schema", "s.json", "--extraction-mode", "bogus"])

    def test_invalid_ocr_engine_rejected(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["extract", "f.pdf", "--schema", "s.json", "--ocr-engine", "bogus"])


# ---------------------------------------------------------------------------
# Config file
# ---------------------------------------------------------------------------

class TestConfig:
    def test_save_and_load_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setattr("clichefactory._config._CONFIG_DIR", tmp_path)
        monkeypatch.setattr("clichefactory._config._CONFIG_FILE", tmp_path / "config.toml")

        cfg = CLIConfig(
            default_mode="local",
            service=ServiceConfig(api_key="cliche-abc123", base_url="https://api.example.com"),
            local=LocalConfig(
                model="openai/gpt-4o",
                api_key="sk-test",
                ocr_model="openai/gpt-4o-mini",
                ocr_api_key="sk-ocr",
            ),
        )
        save_config(cfg)

        loaded = load_config()
        assert loaded.default_mode == "local"
        assert loaded.service.api_key == "cliche-abc123"
        assert loaded.service.base_url == "https://api.example.com"
        assert loaded.local.model == "openai/gpt-4o"
        assert loaded.local.api_key == "sk-test"
        assert loaded.local.ocr_model == "openai/gpt-4o-mini"
        assert loaded.local.ocr_api_key == "sk-ocr"

    def test_load_missing_file_returns_defaults(self, tmp_path, monkeypatch):
        monkeypatch.setattr("clichefactory._config._CONFIG_DIR", tmp_path)
        monkeypatch.setattr("clichefactory._config._CONFIG_FILE", tmp_path / "nonexistent.toml")

        cfg = load_config()
        assert cfg.default_mode == "service"
        assert cfg.service.api_key == ""
        assert cfg.local.model == ""

    def test_save_without_ocr_model_omits_section(self, tmp_path, monkeypatch):
        monkeypatch.setattr("clichefactory._config._CONFIG_DIR", tmp_path)
        monkeypatch.setattr("clichefactory._config._CONFIG_FILE", tmp_path / "config.toml")

        cfg = CLIConfig(
            default_mode="service",
            service=ServiceConfig(api_key="cliche-key"),
            local=LocalConfig(model="gemini/gemini-3-flash-preview", api_key="key"),
        )
        save_config(cfg)

        content = (tmp_path / "config.toml").read_text()
        assert "ocr_model" not in content


# ---------------------------------------------------------------------------
# Config resolution (precedence)
# ---------------------------------------------------------------------------

class TestConfigResolution:
    def test_cli_flag_wins_over_env_and_config(self, monkeypatch):
        from clichefactory._config import resolve_api_key

        monkeypatch.setenv("CLICHEFACTORY_API_KEY", "env-key")
        cfg = CLIConfig(service=ServiceConfig(api_key="config-key"))

        assert resolve_api_key(cli_flag="cli-key", cfg=cfg) == "cli-key"

    def test_env_wins_over_config(self, monkeypatch):
        from clichefactory._config import resolve_api_key

        monkeypatch.setenv("CLICHEFACTORY_API_KEY", "env-key")
        cfg = CLIConfig(service=ServiceConfig(api_key="config-key"))

        assert resolve_api_key(cli_flag=None, cfg=cfg) == "env-key"

    def test_config_used_when_no_cli_or_env(self, monkeypatch):
        from clichefactory._config import resolve_api_key

        monkeypatch.delenv("CLICHEFACTORY_API_KEY", raising=False)
        cfg = CLIConfig(service=ServiceConfig(api_key="config-key"))

        assert resolve_api_key(cli_flag=None, cfg=cfg) == "config-key"

    def test_model_resolution_precedence(self, monkeypatch):
        from clichefactory._config import resolve_model

        monkeypatch.delenv("CLICHEFACTORY_LLM_MODEL_NAME", raising=False)
        monkeypatch.delenv("LLM_MODEL_NAME", raising=False)

        cfg = CLIConfig(local=LocalConfig(model="config-model"))
        assert resolve_model(cli_flag="cli-model", cfg=cfg) == "cli-model"
        assert resolve_model(cli_flag=None, cfg=cfg) == "config-model"

        monkeypatch.setenv("LLM_MODEL_NAME", "env-model")
        assert resolve_model(cli_flag=None, cfg=cfg) == "env-model"

    def test_ocr_key_falls_back_to_model_key(self, monkeypatch):
        from clichefactory._config import resolve_ocr_api_key

        monkeypatch.delenv("CLICHEFACTORY_OCR_API_KEY", raising=False)
        monkeypatch.delenv("OCR_API_KEY", raising=False)
        cfg = CLIConfig(local=LocalConfig(ocr_api_key=""))

        result = resolve_ocr_api_key(cli_flag=None, cfg=cfg, model_api_key="main-key")
        assert result == "main-key"


# ---------------------------------------------------------------------------
# Doctor command
# ---------------------------------------------------------------------------

class TestDoctor:
    def test_doctor_runs_without_error(self, capsys, tmp_path, monkeypatch):
        monkeypatch.setattr("clichefactory._config._CONFIG_DIR", tmp_path)
        monkeypatch.setattr("clichefactory._config._CONFIG_FILE", tmp_path / "config.toml")

        parser = build_parser()
        args = parser.parse_args(["doctor"])

        # doctor may exit with 0 or 1 depending on deps; just check it doesn't crash
        try:
            cmd_doctor(args)
        except SystemExit:
            pass

        output = capsys.readouterr().out
        assert "ClicheFactory Doctor" in output
        assert "Summary:" in output


# ---------------------------------------------------------------------------
# Extract command (with mocked SDK)
# ---------------------------------------------------------------------------

class TestExtractCommand:
    def test_extract_missing_file_exits(self, capsys):
        with pytest.raises(SystemExit):
            main(["extract", "/nonexistent/file.pdf", "--schema", "schema.json"])

    def test_extract_missing_schema_exits(self, tmp_path, capsys):
        doc = tmp_path / "test.pdf"
        doc.write_bytes(b"%PDF-1.4")
        with pytest.raises(SystemExit):
            main(["extract", str(doc), "--schema", "/nonexistent/schema.json"])

    def test_extract_invalid_schema_json_exits(self, tmp_path, capsys):
        doc = tmp_path / "test.pdf"
        doc.write_bytes(b"%PDF-1.4")
        schema = tmp_path / "schema.json"
        schema.write_text("{invalid", encoding="utf-8")
        with pytest.raises(SystemExit):
            main(["extract", str(doc), "--schema", str(schema)])
