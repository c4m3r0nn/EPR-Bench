"""The agent-CLI harness after the first live smokes: credentials, reproducible presets, context exhaustion, clock."""

import json
import sys

import pytest

from eprbench.agents import cli_agent
from tests.test_isolation import agent

CREDENTIALS = {"ANTHROPIC_API_KEY": "sk-ant-planted-0000", "ANTHROPIC_AUTH_TOKEN": "planted-token-0000", "OPENAI_API_KEY": "sk-planted-0000",
               "GEMINI_API_KEY": "AIza-planted-0000", "GOOGLE_API_KEY": "AIza-planted-1111", "CLAUDE_CODE_USE_BEDROCK": "1"}  # fmt: skip

SHOW = "import os, json\nprint(json.dumps(sorted(k for k in " + repr(sorted(CREDENTIALS)) + " if k in os.environ)))\n"


def only_dir(root):
    (d,) = [p for p in root.iterdir() if p.is_dir()]
    return d


def test_provider_credentials_are_withheld_from_the_agent_by_default(tmp_path, monkeypatch):
    for k, v in CREDENTIALS.items():
        monkeypatch.setenv(k, v)
    r = cli_agent.run_episode("find-result-001", agent(tmp_path, SHOW, header=""), isolate=False, out_dir=tmp_path / "out")
    assert json.loads(r["answer"]) == []
    meta = (only_dir(tmp_path / "out") / "episode.json").read_text()
    auth = json.loads(meta)["settings"]["auth"]
    assert auth["mode"] == "subscription" and set(CREDENTIALS) <= set(auth["withheld"])
    assert not any(v in meta for v in CREDENTIALS.values() if len(v) > 1)


def test_credentials_pass_through_only_when_asked(tmp_path, monkeypatch):
    for k, v in CREDENTIALS.items():
        monkeypatch.setenv(k, v)
    r = cli_agent.run_episode("find-result-001", agent(tmp_path, SHOW, header=""), isolate=False, pass_credentials=True)
    assert json.loads(r["answer"]) == sorted(CREDENTIALS) and r["auth"]["mode"] == "api_key" and r["auth"]["withheld"] == []
    assert set(CREDENTIALS) <= set(r["auth"]["passed"])


def test_presets_pin_model_and_effort_and_shut_out_user_configuration():
    claude = cli_agent.preset_argv("claude", sandboxed=True, model="claude-opus-5-5", effort="high")
    assert claude[claude.index("--model") + 1] == "claude-opus-5-5" and claude[claude.index("--effort") + 1] == "high"
    assert {"--strict-mcp-config", "--disable-slash-commands", "--debug-file"} <= set(claude) and "--safe-mode" not in claude  # it drops MCP servers
    codex = cli_agent.preset_argv("codex", sandboxed=True, model="gpt-6", effort="high")
    assert codex[codex.index("-m") + 1] == "gpt-6" and 'model_reasoning_effort="high"' in codex
    assert {"--ignore-user-config", "--ignore-rules"} <= set(codex)
    for feature in ("plugins", "apps", "hooks", "skill_search", "browser_use", "in_app_browser", "computer_use"):
        assert codex[codex.index(feature) - 1] == "--disable"
    # not asked for: not passed, so the CLI's own default applies, and the record says so
    assert "--model" not in cli_agent.preset_argv("claude", sandboxed=True, model=None, effort=None)
    assert "-m" not in cli_agent.preset_argv("codex", sandboxed=True, model=None, effort=None)


def test_the_record_states_model_effort_and_cli_version(tmp_path):
    events = [{"type": "system", "subtype": "init", "model": "claude-opus-5-5"}, {"type": "result", "subtype": "success", "result": "Hb 109 g/L"}]
    body = "".join(f"print({json.dumps(json.dumps(e))})\n" for e in events)
    r = cli_agent.run_episode("find-result-001", agent(tmp_path, body, header=""), isolate=False, model="claude-opus-5-5", effort="high")
    cfg = r["agent_config"]
    assert cfg["model"]["requested"] == cfg["model"]["observed"] == "claude-opus-5-5" and cfg["effort"]["requested"] == "high"
    assert "custom command" in cfg["model"]["note"] and "inherited" in cfg["model"]["note"] and cfg["cli_version"].startswith("Python")
    r = cli_agent.run_episode("find-result-001", agent(tmp_path, body, header=""), isolate=False)
    assert r["agent_config"]["model"]["requested"] is None and "inherited" in r["agent_config"]["model"]["note"]


@pytest.mark.parametrize(
    "events",
    [
        [{"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "Prompt is too long"}],
        [{"type": "turn.failed", "error": {"message": "Your input exceeds the context window of this model (context_length_exceeded)"}}],
    ],
)
def test_context_exhaustion_reported_by_a_cli_is_a_context_limit(tmp_path, events):
    body = "import sys\n" + "".join(f"print({json.dumps(json.dumps(e))})\n" for e in events) + "sys.exit(1)\n"
    r = cli_agent.run_episode("find-result-001", agent(tmp_path, body, header=""), isolate=False)
    assert r["terminal_reason"] == "context_limit" and r["truncated"]


def test_the_cli_clock_is_described_as_it_is(tmp_path):
    r = cli_agent.run_episode("find-result-001", agent(tmp_path, "print('Hb 109 g/L')\n", header=""), isolate=False)
    comparability = r["comparability"]
    assert "launch" in comparability["timer_starts"] and "first model request" not in comparability["timer_starts"]
    assert comparability["action_unit"] == "one API operation call" and comparability["setup_seconds"] is not None


def test_run_passes_agent_settings(monkeypatch, capsys):
    from eprbench.__main__ import main

    seen = {}
    monkeypatch.setattr(cli_agent, "run_episode", lambda task_id, agent, **kw: seen.update(kw) or {"task_id": task_id, "answer": "x"})
    monkeypatch.setattr(sys, "argv", ["eprbench", "run", "find-result-001", "--agent", "codex", "--agent-model", "gpt-6", "--agent-effort", "high",
                                      "--agent-api-keys"])  # fmt: skip
    main()
    assert seen["model"] == "gpt-6" and seen["effort"] == "high" and seen["pass_credentials"] is True
    capsys.readouterr()
