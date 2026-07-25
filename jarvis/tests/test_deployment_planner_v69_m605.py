"""V69 M60.5 — Windows deployment planning and dry-run.

The central assertion is NEGATIVE: the planner must not mutate the host. That is
proved structurally (the module imports no mutation API and defines no apply path)
and behaviourally (every plan reports dry_run, apply_supported=False, host_changes=0).
"""
from __future__ import annotations

import inspect
import json

import pytest

import core.deployment_planner as dp
from core.deployment_planner import (
    DeploymentTarget, EnvironmentFacts, PlanFeasibility, apply_deployment_command,
    build_plan, inspect_environment, known_deployment_aliases,
    parse_deployment_command, render_deployment_status, render_plan,
)


def _facts(**kw) -> EnvironmentFacts:
    base = dict(python_executable="C:\\venv\\Scripts\\python.exe",
                python_version="3.11.9", in_virtualenv=True, venv_name="venv",
                repo_root_name="jarvis", repo_exists=True, entrypoint_exists=True,
                data_writable=True, logs_writable=True,
                ollama_host="http://127.0.0.1:11434", ollama_reachable=True,
                is_windows=True, is_admin=False, pythonw_available=True,
                user_present=True)
    base.update(kw)
    return EnvironmentFacts(**base)


# ══════════════════════════════════════════════════════════════════════════════
#  The planner mutates nothing — structural proof
# ══════════════════════════════════════════════════════════════════════════════
class TestNoMutation:
    def test_module_has_no_apply_path(self):
        names = {n for n in dir(dp) if not n.startswith("_")}
        for forbidden in ("apply_plan", "install_service", "create_task",
                          "install", "deploy", "enable_startup"):
            assert forbidden not in names

    def test_source_contains_no_mutation_api(self):
        # Call/import shapes, not bare words: "SeCreateServicePrivilege" is a
        # permission NAME the plan reports, not an API the planner invokes.
        src = inspect.getsource(dp)
        for banned in ("import winreg", "import subprocess", "os.system(",
                       "ShellExecuteW", "CreateServiceW", "os.environ[",
                       "setx ", "Popen(", "os.remove(", "shutil.rmtree("):
            assert banned not in src, banned

    def test_source_never_runs_a_generated_command(self):
        src = inspect.getsource(dp)
        # Templates exist as STRINGS for the operator to read; nothing executes them.
        assert "schtasks" in src and "sc create" in src
        assert "subprocess" not in src and "shell=True" not in src

    @pytest.mark.parametrize("target", list(DeploymentTarget))
    def test_every_plan_is_dry_run_only(self, target):
        plan = build_plan(target, _facts())
        assert plan.dry_run is True
        assert plan.apply_supported is False
        assert plan.host_changes == 0

    def test_inspection_creates_no_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dp, "data_dir", lambda **kw: tmp_path / "nope")
        inspect_environment(probe_ollama=False)
        assert not (tmp_path / "nope").exists()

    def test_probe_is_bounded_and_never_sends_a_request(self):
        src = inspect.getsource(dp._probe_host)
        assert "create_connection" in src and "timeout=" in src
        # Connect only: no bytes are written and no HTTP client is constructed.
        for banned in (".send(", ".sendall(", ".write(", "httpx", "urllib",
                       "requests."):
            assert banned not in src, banned
        # An unreachable host answers False rather than hanging.
        assert dp._probe_host("http://127.0.0.1:1") is False

    def test_probe_handles_malformed_host(self):
        assert dp._probe_host("") is False
        assert dp._probe_host("not a url at all::::") is False


# ══════════════════════════════════════════════════════════════════════════════
#  Environment inspection
# ══════════════════════════════════════════════════════════════════════════════
class TestInspection:
    def test_inspection_reports_real_interpreter(self):
        facts = inspect_environment(probe_ollama=False)
        assert facts.python_executable
        assert facts.python_version.count(".") == 2
        assert facts.repo_exists is True and facts.entrypoint_exists is True

    def test_admin_check_is_read_only(self):
        src = inspect.getsource(dp._is_admin)
        assert "IsUserAnAdmin" in src
        assert "ShellExecute" not in src and "runas" not in src
        assert isinstance(dp._is_admin(), bool)

    def test_snapshot_is_json_ready(self):
        facts = inspect_environment(probe_ollama=False)
        json.dumps(facts.snapshot())


# ══════════════════════════════════════════════════════════════════════════════
#  Plan content
# ══════════════════════════════════════════════════════════════════════════════
class TestPlans:
    def test_terminal_plan_needs_no_admin(self):
        plan = build_plan(DeploymentTarget.INTERACTIVE_TERMINAL, _facts())
        assert plan.requires_admin is False
        assert plan.executable.endswith("python.exe")
        assert plan.arguments and plan.arguments[0].endswith("main.py")
        assert plan.working_directory

    def test_startup_plan_is_per_user_and_lists_rollback(self):
        plan = build_plan(DeploymentTarget.STARTUP_APPLICATION, _facts())
        assert plan.requires_admin is False
        assert any("Startup" in s for s in plan.manual_steps)
        assert plan.rollback_steps

    def test_startup_plan_warns_without_pythonw(self):
        plan = build_plan(DeploymentTarget.STARTUP_APPLICATION,
                          _facts(pythonw_available=False))
        assert any("pythonw" in w for w in plan.warnings)

    def test_scheduler_plan_shows_the_exact_command_and_rollback(self):
        plan = build_plan(DeploymentTarget.TASK_SCHEDULER_PLAN, _facts())
        assert any("schtasks /Create" in s for s in plan.manual_steps)
        assert any("schtasks /Delete" in s for s in plan.rollback_steps)
        assert any("plan only" in w for w in plan.warnings)

    def test_scheduler_plan_names_the_interactivity_risk(self):
        plan = build_plan(DeploymentTarget.TASK_SCHEDULER_PLAN, _facts())
        joined = " ".join(plan.risks).lower()
        assert "hitl" in joined and "voice" in joined

    def test_service_plan_requires_admin_and_reports_it(self):
        plan = build_plan(DeploymentTarget.WINDOWS_SERVICE_PLAN, _facts())
        assert plan.requires_admin is True
        assert "Administrator" in plan.required_permissions
        assert any("session 0" in r.lower() for r in plan.risks)

    def test_service_plan_warns_when_not_elevated(self):
        plan = build_plan(DeploymentTarget.WINDOWS_SERVICE_PLAN,
                          _facts(is_admin=False))
        assert any("not elevated" in w for w in plan.warnings)

    def test_missing_entrypoint_blocks_the_plan(self):
        plan = build_plan(DeploymentTarget.INTERACTIVE_TERMINAL,
                          _facts(entrypoint_exists=False))
        assert plan.feasibility is PlanFeasibility.BLOCKED

    def test_unreachable_ollama_is_a_warning_not_a_block(self):
        plan = build_plan(DeploymentTarget.INTERACTIVE_TERMINAL,
                          _facts(ollama_reachable=False))
        assert plan.feasibility is PlanFeasibility.READY_WITH_WARNINGS
        assert any("Ollama" in w for w in plan.warnings)

    def test_clean_environment_is_ready(self):
        plan = build_plan(DeploymentTarget.INTERACTIVE_TERMINAL, _facts())
        assert plan.feasibility is PlanFeasibility.READY

    def test_plans_list_managed_data_and_log_paths(self):
        plan = build_plan(DeploymentTarget.INTERACTIVE_TERMINAL, _facts())
        assert any("data" in p for p in plan.data_paths)
        assert any("logs" in p for p in plan.log_paths)

    def test_snapshot_is_json_ready(self):
        for target in DeploymentTarget:
            json.dumps(build_plan(target, _facts()).snapshot())


# ══════════════════════════════════════════════════════════════════════════════
#  Command surface
# ══════════════════════════════════════════════════════════════════════════════
class TestDeploymentCommands:
    @pytest.mark.parametrize("alias,target", [
        ("/deployment-status", None),
        ("/deployment-plan terminal", DeploymentTarget.INTERACTIVE_TERMINAL),
        ("/deployment-plan startup", DeploymentTarget.STARTUP_APPLICATION),
        ("/deployment-plan scheduler", DeploymentTarget.TASK_SCHEDULER_PLAN),
        ("/deployment-plan service", DeploymentTarget.WINDOWS_SERVICE_PLAN),
    ])
    def test_every_required_command_parses(self, alias, target):
        ok, parsed = parse_deployment_command(alias)
        assert ok is True and parsed is target

    @pytest.mark.parametrize("text", [
        "/deployment-plan C:\\evil",
        "/deployment-plan terminal --apply",
        "/deployment-plan ../../..",
        "/deployment-apply service",
        "/deployment-plan",
        "deployment-status",
        "por favor /deployment-plan service",
        "",
    ])
    def test_arbitrary_plan_names_and_apply_are_not_commands(self, text):
        ok, parsed = parse_deployment_command(text)
        assert ok is False and parsed is None

    def test_alias_list_is_closed_and_unique(self):
        aliases = known_deployment_aliases()
        assert len(set(aliases)) == len(aliases)
        assert all(a.startswith("/") for a in aliases)
        assert not any("apply" in a for a in aliases)

    def test_case_and_whitespace_normalised(self):
        ok, target = parse_deployment_command("  /DEPLOYMENT-PLAN   SERVICE ")
        assert ok and target is DeploymentTarget.WINDOWS_SERVICE_PLAN

    def test_apply_command_renders_status(self):
        out = apply_deployment_command(None, facts=_facts())
        assert "DESPLIEGUE" in out or "DEPLOYMENT" in out
        assert "solo lectura" in out or "read-only" in out

    @pytest.mark.parametrize("target", list(DeploymentTarget))
    def test_apply_command_renders_plan_without_mutation(self, target):
        out = apply_deployment_command(target, facts=_facts(), language="en")
        assert "apply_supported=no" in out and "host_changes=0" in out
        assert "nothing was installed, scheduled or registered" in out

    def test_command_never_raises(self):
        assert isinstance(apply_deployment_command(None, facts=None), str)


# ══════════════════════════════════════════════════════════════════════════════
#  Rendering safety
# ══════════════════════════════════════════════════════════════════════════════
class TestRendering:
    @pytest.mark.parametrize("language", ["es", "en"])
    def test_panels_are_cp1252_safe(self, language):
        render_deployment_status(_facts(), language=language).encode("cp1252")
        for target in DeploymentTarget:
            render_plan(build_plan(target, _facts()),
                        language=language).encode("cp1252")

    def test_console_panel_keeps_real_paths_so_steps_are_actionable(self):
        # The interactive panel prints to the operator's OWN console; a manual step
        # they cannot copy is useless. Redaction is for output that leaves the host.
        facts = _facts(python_executable="C:\\Users\\aleja\\venv\\python.exe")
        out = render_plan(build_plan(DeploymentTarget.INTERACTIVE_TERMINAL, facts))
        assert "python.exe" in out

    def test_redacted_render_removes_the_home_path(self):
        facts = _facts(python_executable="C:\\Users\\aleja\\venv\\python.exe")
        plan = build_plan(DeploymentTarget.STARTUP_APPLICATION, facts)
        out = render_plan(plan, redact_home=True)
        assert "aleja" not in out and "<HOME>" in out

    def test_redacted_snapshot_removes_the_home_path(self):
        facts = _facts(python_executable="C:\\Users\\aleja\\venv\\python.exe")
        plan = build_plan(DeploymentTarget.STARTUP_APPLICATION, facts)
        raw = json.dumps(plan.snapshot(redact_home=True))
        assert "aleja" not in raw
        assert "aleja" in json.dumps(plan.snapshot())

    def test_status_reports_measured_values_only(self):
        out = render_deployment_status(_facts(ollama_reachable=False))
        assert "ollama_reachable=no" in out
