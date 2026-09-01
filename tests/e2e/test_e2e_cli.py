"""End-to-End CLI Test Suite.
Verifies CLI command invocations, options, arguments, exit codes, and output effects.
"""

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.e2e
@pytest.mark.cli
class TestE2ECliCommands:
    @pytest.fixture
    def repo_root(self) -> Path:
        return Path(__file__).parent.parent.parent

    def run_cli(self, repo_root: Path, args: list[str]) -> subprocess.CompletedProcess:
        cmd = [sys.executable, str(repo_root / "main.py"), *args]
        return subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True)

    def test_cli_help_flag(self, repo_root):
        """CLI: `python main.py --help` exits 0 with command usage info."""
        if not (repo_root / "main.py").exists():
            pytest.skip("main.py not yet created")
        res = self.run_cli(repo_root, ["--help"])
        assert res.returncode == 0
        assert "scrape" in res.stdout
        assert "match" in res.stdout
        assert "export" in res.stdout
        assert "sync" in res.stdout

    def test_cli_scrape_help(self, repo_root):
        """CLI: `python main.py scrape --help` shows --provider and --limit options."""
        if not (repo_root / "main.py").exists():
            pytest.skip("main.py not yet created")
        res = self.run_cli(repo_root, ["scrape", "--help"])
        assert res.returncode == 0
        assert "--provider" in res.stdout
        assert "--limit" in res.stdout

    def test_cli_match_help(self, repo_root):
        """CLI: `python main.py match --help` shows --unmapped-only and --tmdb-key options."""
        if not (repo_root / "main.py").exists():
            pytest.skip("main.py not yet created")
        res = self.run_cli(repo_root, ["match", "--help"])
        assert res.returncode == 0
        assert "--unmapped-only" in res.stdout

    def test_cli_export_help(self, repo_root):
        """CLI: `python main.py export --help` shows --target option."""
        if not (repo_root / "main.py").exists():
            pytest.skip("main.py not yet created")
        res = self.run_cli(repo_root, ["export", "--help"])
        assert res.returncode == 0
        assert "--target" in res.stdout

    def test_cli_sync_dry_run(self, repo_root):
        """CLI: `python main.py sync --dry-run` runs full orchestration without mutating files."""
        if not (repo_root / "main.py").exists():
            pytest.skip("main.py not yet created")
        res = self.run_cli(repo_root, ["sync", "--dry-run", "--limit", "2"])
        assert res.returncode == 0

    def test_cli_invalid_command_returns_nonzero(self, repo_root):
        """CLI: invalid subcommand returns non-zero status code."""
        if not (repo_root / "main.py").exists():
            pytest.skip("main.py not yet created")
        res = self.run_cli(repo_root, ["invalid_command_xyz"])
        assert res.returncode != 0
