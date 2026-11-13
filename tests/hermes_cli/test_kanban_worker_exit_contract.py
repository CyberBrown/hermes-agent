"""Regression tests for the OP #1397 kanban-worker exit-code contract.

The kanban dispatcher spawns workers as
``hermes -p <lane> --cli chat -q "work kanban task <id>"`` — the
*non-quiet* single-query branch of ``cli.main()``. Until #1397 only the
``-Q`` branch mapped the turn result to a process exit code, so every
failure visible on the ``-q`` path (provider budget wall, unknown provider
config, agent-init failure, exception inside ``chat()``) exited rc=0 while
the task row was still ``running``. ``detect_crashed_workers`` classified
that as a clean-exit protocol violation and crash-looped cards — observed
as 8 consecutive failed runs on t_adbf236a (two clusters: gateway 429
"Budget exceeded" on the backend lane, and "Unknown provider 'custom:coder'"
on the lane-coder lane; see ~/.hermes/kanban/logs/t_adbf236a.log).

These tests pin the shared mapper and the branches' wiring:

1. ``_single_query_exit_code`` — pure contract table (0 / 1 / 75).
2. The ``-q`` branch consults it after ``chat()`` (source-level pin, the
   established convention for the inline branch — see
   tests/test_cli_quiet_stdout_leak.py for the same pattern on -Q).
3. ``chat()``'s silent early returns record a failed result dict so the
   mapper can see them.
"""

from __future__ import annotations

from pathlib import Path



import cli as cli_mod


# ── 1. the mapper's contract table ──────────────────────────────────────


class TestSingleQueryExitCode:
    def test_success_is_zero(self, monkeypatch):
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        assert cli_mod._single_query_exit_code(
            {"completed": True, "failed": False}
        ) == 0

    def test_none_result_is_zero(self, monkeypatch):
        # A turn that never produced a dict (e.g. interrupted before any
        # result) must not be reclassified; chat() records explicit
        # failure dicts for its own silent paths.
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        assert cli_mod._single_query_exit_code(None) == 0

    def test_real_failure_exits_one_non_kanban(self, monkeypatch):
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        assert cli_mod._single_query_exit_code(
            {"failed": True, "failure_reason": "rate_limit"}
        ) == 1

    def test_real_failure_exits_one_kanban(self, monkeypatch):
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_deadbeef")
        assert cli_mod._single_query_exit_code(
            {"failed": True, "failure_reason": "exception"}
        ) == 1

    def test_quota_wall_on_kanban_worker_maps_to_sentinel(self, monkeypatch):
        # This is THE case #1397 needed: a kanban worker that died on the
        # provider's budget wall must exit EX_TEMPFAIL, not rc=0. The
        # dispatcher's reap classifier turns 75 into a rate_limited release
        # that requeues WITHOUT consuming the protocol-violation budget.
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_adbf236a")
        from hermes_cli.kanban_db import KANBAN_RATE_LIMIT_EXIT_CODE

        for reason in ("rate_limit", "billing"):
            got = cli_mod._single_query_exit_code(
                {"failed": True, "failure_reason": reason}
            )
            assert got == KANBAN_RATE_LIMIT_EXIT_CODE, reason
            assert got == 75

    def test_completed_false_without_failed_flag_is_zero(self, monkeypatch):
        # An exhausted-iteration turn completes with completed=False,
        # failed unset — the old -Q branch treated it as success (rc=0).
        # Preserve that: the worker DID run; it just stopped short.
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_deadbeef")
        assert cli_mod._single_query_exit_code(
            {"completed": False, "api_calls": 270}
        ) == 0


# ── 2. both single-query branches use the shared mapper ─────────────────


def _main_source() -> str:
    return Path(cli_mod.__file__).read_text(encoding="utf-8")


class TestBranchWiring:
    def test_quiet_branch_uses_mapper(self):
        source = _main_source()
        assert "sys.exit(_single_query_exit_code(result))" in source

    def test_plain_query_branch_uses_turn_result(self):
        # The dispatcher's spawn shape (`--cli ... chat -q`) lands in the
        # NON-quiet branch; it must route chat()'s recorded result through
        # the same mapper instead of falling through to rc=0.
        source = _main_source()
        assert 'getattr(cli, "_last_turn_result", None)' in source
        assert "_sq_exit_code = _single_query_exit_code(" in source

    def test_mapper_defined_exactly_once(self):
        source = _main_source()
        assert source.count("def _single_query_exit_code(") == 1


# ── 3. chat()'s silent paths record a failure dict ──────────────────────


class _StubCli:
    """Minimal stand-in bound to the real HermesCLI methods under test."""

    _last_turn_result = None
    _last_turn_interrupted = False

    def __init__(self, creds_ok: bool = True):
        self._creds_ok = creds_ok

    def _ensure_runtime_credentials(self):
        return self._creds_ok

    def _secret_capture_callback(self):  # pragma: no cover - never called
        pass


class TestChatRecordsFailures:
    def test_credential_failure_records_auth_failure(self, monkeypatch):
        monkeypatch.setattr(cli_mod, "set_secret_capture_callback", lambda cb: None)
        stub = _StubCli(creds_ok=False)
        from cli import HermesCLI

        got = HermesCLI.chat(stub, "work kanban task t_x")  # type: ignore[arg-type]
        assert got is None
        rec = stub._last_turn_result
        assert isinstance(rec, dict)
        assert rec.get("failed") is True
        assert rec.get("failure_reason") == "auth"
        assert cli_mod._single_query_exit_code(rec) == 1

    def test_credential_failure_on_kanban_worker_still_exits_one(self, monkeypatch):
        # An auth failure is NOT a quota wall — even on a kanban worker it
        # must exit 1, so the dispatcher records a real crash rather than
        # silently deferring forever on the respawn guard.
        monkeypatch.setattr(cli_mod, "set_secret_capture_callback", lambda cb: None)
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_adbf236a")
        stub = _StubCli(creds_ok=False)
        from cli import HermesCLI

        HermesCLI.chat(stub, "work kanban task t_x")  # type: ignore[arg-type]
        assert cli_mod._single_query_exit_code(stub._last_turn_result) == 1

    def test_source_records_init_failures(self):
        # Full chat() execution needs a live agent; pin the recording at
        # source level instead (same convention as the -Q stdout-leak tests).
        source = _main_source()
        assert '"Agent initialization failed"' in source
        assert '"Agent unavailable after init"' in source
        assert '"Provider/credential resolution failed"' in source
