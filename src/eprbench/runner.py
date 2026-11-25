"""Running SDK episodes from the command line: tasks one after another, or a counterbalanced plan, always metered.

No paid SDK episode runs without a spend budget (eprbench.spend: an initialised ledger, a prices file listing the
model): `run_one` refuses, unless the caller has explicitly accepted unmetered spend, which is recorded in the episode.

A plan (`eprbench plan`) fixes, before anything runs, every episode of a paired tool-versus-UI comparison: for each task
and model, `repeats` pairs, each pair one episode per interface, run one after the other. Within each task and model
the interface that goes first alternates from pair to pair (from a seeded random start), and the pairs are run in a
seeded random order, so neither interface always runs first, early or late. Each episode pins its provider, model id,
reasoning effort, output bound and limits, and the plan pins what the episodes depend on: the SHA-256 of every task
(as loaded, and its file), of every bundle and catalogue, of the grader's files and of the prices file. `eprbench run
--plan FILE --out-dir DIR` refuses to start if any of them has changed, skips every episode whose evidence under DIR has
a valid completion manifest (eprbench.episode.write_manifest), and stops if the provider reports a different model
version from the one earlier episodes of the plan reported for the same model id.

A plan for agent CLIs (`--agents claude codex`, interfaces api and ui) is the same design for the agent products: it
names no prices or ledger, since a subscription CLI is not metered here, and no paid SDK client is made to run it (the
agent's own usage is recorded where its CLI reports it). Agent-CLI and SDK episodes never share a plan: they measure
different things (a product and a bare model) and are reported separately.
"""

import json
import random
import re
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from eprbench.agents import UnmeteredRefused
from eprbench.episode import COMPLETE_ENDINGS, RUN_CONTEXT, Limits, grader_files, manifest_problems
from eprbench.loader import bundle_digests, file_sha256
from eprbench.tasks import list_tasks, load_task, task_file

SUPPORTED = {("anthropic", "tool"), ("anthropic", "ui"), ("openai", "tool"), ("openai", "ui"), ("gemini", "tool")}
AGENTS = ("claude", "codex")  # agent CLIs (eprbench.agents.cli_agent, cli_browser): subscription runs, never metered here
AGENT_INTERFACES = ("api", "ui")
DEFAULT_MODELS = {"anthropic": "claude-opus-5-5", "gemini": "gemini-3.8-flash", "openai": "gpt-5.4-mini"}
DEFAULT_MAX_OUTPUT = 16000  # the Claude harnesses' own default; the OpenAI ones send a bound only when one is given
PLAN_FORMAT = "eprbench-plan/2"
REASONING = {"anthropic": "output_config.effort", "openai": "reasoning.effort", "gemini": None}  # how each harness sends the effort


class Clients:
    """Provider SDK clients, made when first needed (each reads its own API key from the environment)."""

    def __init__(self):
        self._made: dict[str, object] = {}

    def __call__(self, provider: str):
        if provider not in self._made:
            if provider == "openai":
                from openai import OpenAI

                self._made[provider] = OpenAI()
            elif provider == "gemini":
                from google import genai

                self._made[provider] = genai.Client()
            else:
                import anthropic

                self._made[provider] = anthropic.Anthropic()
        return self._made[provider]


def effective_effort(provider: str, effort: str | None) -> str | None:
    """The reasoning effort a harness actually sends for this provider: None where its harness sends none (Gemini)."""
    return effort if REASONING[provider] else None


def run_one(task_id: str, provider: str, model: str, interface: str, *, client, effort: str, repeat: int, limits: Limits, out_dir=None,
            spend=None, max_output_tokens: int | None = None, unmetered: bool = False) -> dict:  # fmt: skip
    """One episode through the SDK harness for this provider and interface. Refused without a spend budget unless
    `unmetered` (the caller's explicit acceptance, recorded in the episode)."""
    from eprbench.env import EPREnv

    if spend is None and not unmetered:
        raise UnmeteredRefused("a paid episode needs a spend budget (--ledger and --prices), or --i-accept-unmetered-spend")
    if spend is not None and provider == "gemini":
        raise UnmeteredRefused("the Gemini harness has no bounded output, so no request of it can be priced in advance")
    common = {"limits": limits, "out_dir": out_dir, "repeat": repeat} | ({"spend": spend} if spend is not None else {"unmetered": True})
    if provider == "openai":
        from eprbench.agents import openai_browser, openai_tools

        bound = ({"max_output_tokens": max_output_tokens} if max_output_tokens else {}) | ({"effort": effort} if effort else {})
        if interface == "ui":
            return openai_browser.run_episode(task_id, client, model, **common, **bound)
        return openai_tools.run_episode(EPREnv(task_id), client, model, **common, **bound)
    if provider == "gemini":
        from eprbench.agents import gemini_tools

        return gemini_tools.run_episode(EPREnv(task_id), client, model, **common)
    bound = {"max_tokens": max_output_tokens} if max_output_tokens else {}
    if interface == "tool":
        from eprbench.agents import claude_tools

        return claude_tools.run_episode(EPREnv(task_id), client, model, effort, **common, **bound)
    from eprbench.agents import claude_browser

    return claude_browser.run_episode(task_id, client, model, effort, **common, **bound)


# --- the plan -----------------------------------------------------------------------------------------------------


def pins(tasks: list[str], prices_sha256: str | None) -> dict:
    """What a plan's episodes depend on, by SHA-256: each task as loaded and its file, each bundle and the catalogues it
    includes, the grader's files and the prices file."""
    from eprbench.episode import _sha256

    from importlib import resources

    pkg = Path(str(resources.files("eprbench")))
    loaded = {t: load_task(t) for t in tasks}
    return {
        "tasks": {t: {"task_sha256": _sha256(task.model_dump_json()), "file_sha256": file_sha256(task_file(t))} for t, task in loaded.items()},
        "bundles": {b: bundle_digests(b) for b in sorted({task.bundle for task in loaded.values()})},
        "grader_sha256": {name: file_sha256(pkg / name) for name in grader_files(pkg)},
        "prices_sha256": prices_sha256,
    }


PINNED = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2})$")  # a dated snapshot id (claude-...-20250929, gpt-...-2025-08-07): not a moving alias


def make_plan(tasks: list[str], interfaces: list[str], models: list[tuple[str, str]], repeats: int, seed: int, *, effort: str | None, limits: Limits,
              max_output_tokens: int | None, prices_sha256: str | None = None, ledger_id: str | None = None, allow_alias: bool = False,
              agents: list[tuple[str, str | None]] | None = None) -> dict:  # fmt: skip
    """A seeded, counterbalanced run order (see the module's docstring). Model ids must be dated snapshots, which do not
    move, unless `allow_alias` (recorded in the plan); the plan names the ledger it is to spend against.

    With `agents` ((cli, model or None) pairs, and no `models`) the plan is for agent CLIs: their interfaces are api and
    ui, `effort` is what each CLI is told (None: its own default), and there are no prices, ledger or output bound."""
    tasks = list_tasks() if tasks == ["all"] else tasks
    if agents and models:
        raise ValueError("an agent CLI and a bare model measure different things: plan them separately")
    if len(set(interfaces)) != len(interfaces) or not interfaces:
        raise ValueError("give each interface once")
    if agents:
        for agent, _ in agents:
            if agent not in AGENTS:
                raise ValueError(f"unknown agent CLI {agent}: one of {', '.join(AGENTS)}")
        if bad := [i for i in interfaces if i not in AGENT_INTERFACES]:
            raise ValueError(f"an agent CLI works the record over the api or ui interface, not {', '.join(bad)}")
        if prices_sha256 or ledger_id:
            raise ValueError("an agent CLI uses its subscription and is not metered here: no prices or ledger")
        units = [("agent", agent, model) for agent, model in agents]
    else:
        if aliases := [m for _, m in models if not PINNED.search(m)]:
            if not allow_alias:
                raise ValueError(f"model ids must be pinned to a dated snapshot, which a provider does not change: {', '.join(aliases)} "
                                 "(or --allow-alias, recorded in the plan)")  # fmt: skip
        for provider, _ in models:
            for interface in interfaces:
                if (provider, interface) not in SUPPORTED:
                    raise ValueError(f"{provider} does not support the {interface} interface")
        units = [(provider, None, model) for provider, model in models]
    rng = random.Random(seed)
    pairs = []
    for task_id in tasks:
        for provider, agent, model in units:
            start = rng.randrange(len(interfaces))  # which interface the first pair of this task and model begins with
            for repeat in range(repeats):
                k = (start + repeat) % len(interfaces)
                pairs.append({"task_id": task_id, "provider": provider, **({"agent": agent} if agent else {}), "model": model, "repeat": repeat,
                              "order": interfaces[k:] + interfaces[:k]})  # fmt: skip
    rng.shuffle(pairs)
    settings = {"limits": asdict(limits)} if agents else {"max_output_tokens": max_output_tokens, "limits": asdict(limits)}
    episodes = []
    for p, pair in enumerate(pairs, 1):
        pair["pair"] = p
        for interface in pair["order"]:
            how = ({"agent": pair["agent"], "effort": effort} if agents
                   else {"effort": effective_effort(pair["provider"], effort), "reasoning_parameter": REASONING[pair["provider"]]})  # fmt: skip
            episodes.append({"n": len(episodes) + 1, "pair": p, "task_id": pair["task_id"], "provider": pair["provider"], "model": pair["model"],
                             "interface": interface, "repeat": pair["repeat"], **how, **settings})  # fmt: skip
    if agents:
        from eprbench.agents.cli_agent import PRESETS, cli_executable, cli_version

        who = {"agents": [f"{a}:{m}" if m else a for a, m in agents], "cli_versions": {a: cli_version(cli_executable(a, PRESETS[a][0])) for a, _ in agents},
               "metering": "none: agent CLIs use their subscription logins; their usage is recorded where they report it"}  # fmt: skip
    else:
        who = {"models": [f"{p}:{m}" for p, m in models], "ledger": {"ledger_id": ledger_id}, "allow_alias": allow_alias}
    return {"format": PLAN_FORMAT, "seed": seed, "tasks": tasks, "interfaces": interfaces, **who, "repeats": repeats, "settings": {"effort": effort, **settings},
            "pins": pins(tasks, prices_sha256), "pairs": pairs, "episodes": episodes}  # fmt: skip


def is_agent_plan(plan: dict) -> bool:
    return any(e["provider"] == "agent" for e in plan["episodes"])


def load_plan(path: str | Path) -> tuple[dict, str]:
    import hashlib

    raw = Path(path).read_bytes()
    plan = json.loads(raw)
    if plan.get("format") != PLAN_FORMAT or not isinstance(plan.get("episodes"), list) or not isinstance(plan.get("pins"), dict):
        raise ValueError(f"{path} is not a plan written by this version of eprbench plan")
    for e in plan["episodes"]:
        if e["provider"] == "agent":
            if e.get("agent") not in AGENTS or e["interface"] not in AGENT_INTERFACES:
                raise ValueError(f"plan episode {e['n']}: agent {e.get('agent')} does not run over the {e['interface']} interface")
        elif (e["provider"], e["interface"]) not in SUPPORTED:
            raise ValueError(f"plan episode {e['n']}: {e['provider']} does not support the {e['interface']} interface")
    if len({e["provider"] == "agent" for e in plan["episodes"]}) > 1:
        raise ValueError(f"{path} mixes agent CLIs and bare models, which are reported separately")
    return plan, hashlib.sha256(raw).hexdigest()


def pin_changes(plan: dict, prices_sha256: str | None) -> list[str]:
    """What has changed since the plan was written (nothing: it may run)."""
    then, now = plan["pins"], pins(plan["tasks"], plan["pins"].get("prices_sha256"))
    out = [f"task {t} changed" for t in then["tasks"] if then["tasks"][t] != now["tasks"].get(t)]
    out += [f"bundle or catalogue {b} changed" for b in then["bundles"] if then["bundles"][b] != now["bundles"].get(b)]
    out += [f"grader file {g} changed" for g in sorted(set(then["grader_sha256"]) | set(now["grader_sha256"])) if then["grader_sha256"].get(g) != now["grader_sha256"].get(g)]
    if prices_sha256 is not None and prices_sha256 != then.get("prices_sha256"):
        out.append("the prices file is not the one the plan was written with")
    return out


def manifests(out_dir: str | Path) -> list[tuple[Path, dict]]:
    out = []
    for meta_file in sorted(Path(out_dir).glob("*/episode.json")) if Path(out_dir).is_dir() else []:
        try:
            out.append((meta_file.parent, json.loads((meta_file.parent / "manifest.json").read_text())))
        except (OSError, ValueError):
            continue
    return out


def completed(out_dir: str | Path, plan_sha256: str) -> dict[int, Path]:
    """The plan's episodes whose evidence under `out_dir` is complete: a manifest naming this plan and saying the episode
    ran to its end (eprbench.episode.COMPLETE_ENDINGS), every file it lists present and matching its hash."""
    return {m["plan"]["n"]: d for d, m in manifests(out_dir)
            if (m.get("plan") or {}).get("sha256") == plan_sha256 and m.get("complete") is True and not manifest_problems(d)}  # fmt: skip


def ledger_problems(plan: dict, plan_sha256: str, out_dir: str | Path, state) -> list[tuple[str | None, str]]:
    """Why this ledger is not the one this plan and its evidence spent against, each as (the other ledger's id, or None,
    what): the plan names another ledger, or an episode's manifest under `out_dir` does (a fresh ledger in place of the
    old), or a manifest witnessed this ledger further on, or at another entry, than it now is (rolled back or
    rewritten). A replacement approved for this plan, from that ledger to this one, is not a problem
    (replacements_approved); a rollback never is approved."""
    approved = replacements_approved(state, plan_sha256)
    out = []
    planned = (plan.get("ledger") or {}).get("ledger_id")
    if planned not in (None, state.ledger_id) and planned not in approved:
        out.append((planned, f"the plan was written for ledger {planned}, not {state.ledger_id}"))
    for d, m in manifests(out_dir):
        led = m.get("ledger")
        if not led:
            continue
        if led.get("ledger_id") != state.ledger_id:
            if led.get("ledger_id") not in approved:
                out.append((led.get("ledger_id"), f"{d.name} spent against ledger {led.get('ledger_id')}, not {state.ledger_id}"))
        elif not (isinstance(led.get("last_seq"), int) and led["last_seq"] <= state.seq and state.hashes[led["last_seq"] - 1] == led.get("last_hash")):
            out.append((None, f"{d.name}'s manifest does not match the ledger at entry {led.get('last_seq')}: the ledger was rolled back or rewritten"))
    return out


def replacements_approved(state, plan_sha256: str) -> set[str]:
    """The ledger ids this ledger was approved to replace for this plan (override entries, --new-ledger-for-existing-run)."""
    out = set()
    for what in state.overrides:
        try:
            o = json.loads(what)
        except ValueError:
            continue
        if isinstance(o, dict) and o.get("replace") == plan_sha256 and o.get("to") == state.ledger_id:
            out.update(o.get("from") or [])
    return out


def approve_replacement(ledger, plan_sha256: str, problems: list[tuple[str | None, str]], reason: str) -> None:
    """Record exactly the replacement the operator approved: these old ledger ids, for this plan, by this ledger."""
    if any(old is None for old, _ in problems):
        raise ValueError("a ledger rolled back or rewritten cannot be approved: " + "; ".join(m for old, m in problems if old is None))
    state = ledger.read()
    what = json.dumps({"replace": plan_sha256, "from": sorted({old for old, _ in problems}), "to": state.ledger_id}, sort_keys=True)
    ledger.record_override(what, f"{reason} [{'; '.join(m for _, m in problems)}]")


def refused_waiting(out_dir: str | Path, plan_sha256: str, n: int, state) -> dict | None:
    """The latest evidence of episode n refused for spend, if the ledger's ceiling has not been raised since."""
    refused = [m for _, m in manifests(out_dir) if (m.get("plan") or {}).get("sha256") == plan_sha256 and m["plan"]["n"] == n
               and m.get("terminal_reason") == "spend_limit" and m.get("ledger")]  # fmt: skip
    if not refused:
        return None
    last = max(m["ledger"]["last_seq"] for m in refused)
    return None if any(r > last for r in state.raised_at) else {"n": n, "ledger_seq": last}


def _reported(d: Path) -> list[str]:
    try:
        return json.loads((d / "result.json").read_text()).get("reported_models") or []
    except (OSError, ValueError):
        return []


def _stop_after(report: dict) -> str | None:
    if report.get("terminal_reason") == "agent_usage_limit":  # nothing more can run until the subscription's limit resets
        message = (report.get("usage_limit") or {}).get("message") or "no reset time reported"
        return (f"the {report.get('agent')} subscription reached its usage limit ({report.get('task_id')}, repeat {report.get('repeat')}): {message}. "
                "Run the plan again after it resets; that episode runs again")  # fmt: skip
    if report.get("terminal_reason") == "spend_limit":
        return f"a request was refused for spend ({report.get('task_id')}, repeat {report.get('repeat')}): raise the ceiling with eprbench ledger raise to go on"
    if drift := report.get("model_drift"):
        return f"{drift['model']} was answered by {drift['reported']}, not {drift['expected']}: the model version changed"
    return None


def first_request(episode: dict) -> dict:
    """The parameters of a plan episode's first request, as its harness sends it (the smallest it will reserve)."""
    from eprbench import answers, ops
    from eprbench.agents import SYSTEM_PROMPT
    from eprbench.agents.claude_browser import ui_note

    task = load_task(episode["task_id"])
    out = episode["max_output_tokens"] or DEFAULT_MAX_OUTPUT
    if episode["interface"] == "ui":  # the instruction, and one screenshot from the first turn on
        from eprbench.spend import MAX_IMAGE_HEIGHT, MAX_IMAGE_WIDTH  # noqa: F401  (the allowance is for a screenshot this size)

        shot = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _blank_png()}}
        return {"system": SYSTEM_PROMPT + ui_note(task), "tools": [{"type": "computer"}], "max_tokens": out,
                "messages": [{"role": "user", "content": [{"type": "text", "text": task.instruction}, shot]}]}  # fmt: skip
    tools = ops.tool_schemas() + [answers.submit_tool(task)]
    return {"system": SYSTEM_PROMPT, "tools": tools, "max_tokens": out, "messages": [{"role": "user", "content": task.instruction}]}


def _blank_png() -> str:
    import base64
    import struct
    import zlib

    from eprbench.spend import MAX_IMAGE_HEIGHT, MAX_IMAGE_WIDTH

    chunk = lambda kind, data: struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))  # noqa: E731
    raw = zlib.compress(b"".join(b"\x00" + b"\x00" * MAX_IMAGE_WIDTH * 3 for _ in range(MAX_IMAGE_HEIGHT)))
    ihdr = struct.pack(">IIBBBBB", MAX_IMAGE_WIDTH, MAX_IMAGE_HEIGHT, 8, 2, 0, 0, 0)
    return base64.b64encode(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")).decode()


GROWTH_TOOL_RESULT_BYTES = 8000  # what one tool result or page adds to the next request, assumed (a results list, a note)
GROWTH_BYTES_PER_OUTPUT_TOKEN = 4  # what one output token adds to the next request as text, assumed


def estimate(plan: dict, prices, ceiling_gbp: float, evidence: str | Path | None = None, growth_bytes: int | None = None) -> dict:
    """What a plan's requests may reserve, per model and interface, under stated assumptions; nothing here is a promise.

    The upper bound if every reservation is kept: request k of an episode carries the first request, k - 1 rounds of
    growth (the model's whole output, at GROWTH_BYTES_PER_OUTPUT_TOKEN bytes a token, and a tool result of
    GROWTH_TOOL_RESULT_BYTES, unless `growth_bytes` is given) and, in the browser, k screenshots, but never more than
    the largest request that may be sent; each reserves that input at the dearest input rate plus the maximum output.
    An episode makes at most its request limit (plus up to 3 final-chance requests). A conversation that grows faster
    than assumed reserves more, up to the per-request size limit. With `evidence`, the expected case from measured
    usage priced at these prices; an episode with any response lacking usable usage is counted apart, never as zero."""
    from eprbench.spend import CLASSES, MAX_REQUEST_BYTES, check_usage, measure, token_classes

    if is_agent_plan(plan):
        raise ValueError("an agent CLI plan spends no money here: there is nothing to estimate")
    models = {}
    for e in plan["episodes"]:
        key = (e["provider"], e["model"], e["interface"])
        if key in models:
            models[key]["episodes_planned"] += 1
            continue
        price = prices.model(e["model"])
        params = first_request(e)
        size = measure(params)
        out = params["max_tokens"]
        growth = growth_bytes if growth_bytes is not None else out * GROWTH_BYTES_PER_OUTPUT_TOKEN + GROWTH_TOOL_RESULT_BYTES
        n = e["limits"]["max_requests"] + (3 if e["limits"]["final_chance"] else 0)
        ui = e["interface"] == "ui"

        def reservation(k: int) -> float:
            text = min(size["text_bytes"] + (k - 1) * growth, MAX_REQUEST_BYTES)
            return (text + (k if ui else size["images"]) * price.max_image_tokens + price.overhead) * price.input_max + out * price.gbp["output"]

        largest = (MAX_REQUEST_BYTES + price.overhead) * price.input_max + out * price.gbp["output"]
        per_request = [min(reservation(k), largest) for k in range(1, n + 1)]
        episode = sum(per_request)
        total, within = 0.0, 0
        for r in per_request:  # how far one fresh episode gets before its reservations reach the ceiling
            if total + r > ceiling_gbp:
                break
            total, within = total + r, within + 1
        models[key] = {
            "provider": e["provider"], "model": e["model"], "interface": e["interface"], "episodes_planned": 1,
            "first_request_reservation_gbp": round(per_request[0], 6), "largest_request_reservation_gbp": round(largest, 6),
            "assumptions": {"requests_per_episode": n, "first_request_bytes": size["text_bytes"], "growth_bytes_per_request": growth,
                            "screenshots_in_request_k": "k" if ui else size["images"], "per_request_size_limit_bytes": MAX_REQUEST_BYTES,
                            "max_output_tokens": out},
            "upper_bound_if_every_reservation_kept": {"episode_gbp": round(episode, 6), "requests_of_one_episode_within_ceiling": within,
                                                      "episodes_within_ceiling": int(ceiling_gbp // episode) if episode else None},
        }  # fmt: skip
    if evidence is not None:
        measured: dict[tuple, list] = {}
        unusable: dict[tuple, int] = {}
        for result in sorted(Path(evidence).glob("*/result.json")):
            r = json.loads(result.read_text())
            key = (r.get("provider"), r.get("model"), r.get("interface"))
            if key not in models:
                continue
            events = [json.loads(x) for x in (result.parent / "transcript.jsonl").read_text().splitlines() if x.strip()]
            usages = [ev.get("usage") for ev in events if ev.get("kind") == "response"]
            sizes = {"text_bytes": 1, "continues": False}
            if not usages or any(check_usage(u, r["provider"], sizes, None) for u in usages):
                unusable[key] = unusable.get(key, 0) + 1  # not priced at all: a missing count is not a zero
                continue
            price = prices.model(r["model"])
            cost = sum(sum(token_classes(u)[c] * price.gbp[c] for c in CLASSES) for u in usages)
            measured.setdefault(key, []).append((len(usages), cost))
        for key, row in models.items():
            runs = measured.get(key, [])
            mean_cost = sum(c for _, c in runs) / len(runs) if runs else None
            row["expected_from_evidence"] = {
                "episodes_measured": len(runs), "episodes_without_usable_usage": unusable.get(key, 0),
                "mean_requests_per_episode": round(sum(n for n, _ in runs) / len(runs), 2) if runs else None,
                "mean_episode_cost_gbp": round(mean_cost, 6) if mean_cost is not None else None,
                "episodes_within_ceiling_at_that_cost": int(ceiling_gbp // mean_cost) if mean_cost else None,
            }  # fmt: skip
    return {"ceiling_gbp": ceiling_gbp, "episodes_planned": len(plan["episodes"]), "models": list(models.values()),
            "note": ("estimates under the stated assumptions, not promises. The upper bound assumes every reservation is kept and the conversation "
                     "grows as stated; a faster-growing one reserves more, up to the per-request size limit. The expected case prices measured "
                     "usage only. The code's ceiling is the second line of defence: set and verify a provider-side hard limit first "
                     "(docs/SPEND_SAFETY.md).")}  # fmt: skip


def run_agent(e: dict, out_dir, **options) -> dict:
    """One plan episode of an agent CLI, over the JSON API or the web UI. `options` go to the harness (allow_unsandboxed,
    protect, pass_credentials)."""
    from eprbench.agents import cli_agent, cli_browser

    harness = cli_browser if e["interface"] == "ui" else cli_agent
    return harness.run_episode(e["task_id"], e["agent"], limits=Limits(**e["limits"]), out_dir=out_dir, repeat=e["repeat"], model=e["model"],
                               effort=e["effort"], **options)  # fmt: skip


def _agent_facts(d: dict) -> tuple[str | None, str | None]:
    """(CLI version, model the agent reported running) from an agent-CLI episode's report or result."""
    config = d.get("agent_config") or {}
    return config.get("cli_version"), (config.get("model") or {}).get("observed")


def agent_drift(plan: dict, facts: list[tuple[dict, dict]]) -> list[str]:
    """How the agent products behind a plan have drifted: a CLI whose version is not the one the plan was written with
    (now, or in an episode's record), or one agent and requested model answered by more than one observed model.
    `facts` pairs each plan episode run so far with its report or result."""
    from eprbench.agents.cli_agent import PRESETS, cli_executable, cli_version

    pinned = plan.get("cli_versions") or {}
    out = [f"the {a} CLI is now {now!r}, the plan was written with {v!r}" for a, v in pinned.items() if (now := cli_version(cli_executable(a, PRESETS[a][0]))) != v]
    models: dict[tuple, set] = {}
    for e, r in facts:
        version, observed = _agent_facts(r)
        if e.get("agent") in pinned and version != pinned[e["agent"]]:
            out.append(f"episode {e['n']} ran {e['agent']} {version!r}, the plan was written with {pinned[e['agent']]!r}")
        if observed:
            models.setdefault((e.get("agent"), e.get("model")), set()).add(observed)
    out += [f"{a} (model asked for: {m or 'its default'}) ran more than one model: {sorted(s)}" for (a, m), s in models.items() if len(s) > 1]
    return list(dict.fromkeys(out))


def _result(d: Path) -> dict:
    try:
        return json.loads((d / "result.json").read_text())
    except (OSError, ValueError):
        return {}


def run_plan(path: str | Path, out_dir: str | Path, clients: Callable | None, emit: Callable[[dict], None], budget=None, unmetered: bool = False,
             agent_options: dict | None = None, accept_drift: str | None = None) -> dict:  # fmt: skip
    """Run a plan's episodes in order, skipping those already complete under `out_dir`. Refuses a plan whose pins have
    changed; stops early, leaving the rest to a later run, when the spend budget could not cover another episode's first
    request, or when a model id is answered by a different model version from the one earlier episodes reported. An
    agent-CLI plan runs with no budget and no SDK client (`clients` is never called for it), and stops on agent drift
    (agent_drift: a CLI version other than the plan's, or a change of observed model), unless `accept_drift` gives the
    reason to go on, which every episode run then records with the drift it accepted."""
    plan, sha = load_plan(path)
    if changed := pin_changes(plan, budget.prices.sha256 if budget is not None else None):
        raise ValueError("the plan no longer matches what it was written for: " + "; ".join(changed))
    if is_agent_plan(plan) and (budget is not None or unmetered):
        raise ValueError("an agent CLI plan is not metered: give it no spend budget")
    if budget is not None:
        budget.ledger.bind_plan(sha)
    done = completed(out_dir, sha)
    versions: dict[tuple[str, str], set[str]] = {}
    for e in plan["episodes"]:
        if e["n"] in done:
            versions.setdefault((e["provider"], e["model"]), set()).update(_reported(done[e["n"]]))
    ran, finished, errored, stopped = 0, 0, {}, None  # ran = completed + errored; left counts every episode not complete
    for key, seen in versions.items():
        if len(seen) > 1:
            stopped = f"{key[1]} was answered by more than one model version: {sorted(seen)}"
    agents = is_agent_plan(plan)
    facts = [(e, _result(done[e["n"]])) for e in plan["episodes"] if e["n"] in done] if agents else []
    accepted: list[str] = []

    def drifted() -> str | None:
        found = agent_drift(plan, facts)
        if found and accept_drift:
            accepted[:] = found
            return None
        return ("agent drift: " + "; ".join(found) + ". Pass --accept-agent-drift REASON to go on (recorded in every episode)") if found else None

    if agents and stopped is None:
        stopped = drifted()
    for e in plan["episodes"] if stopped is None else []:
        if e["n"] in done:
            continue
        if budget is not None and (waiting := refused_waiting(out_dir, sha, e["n"], budget.ledger.read())):
            stopped = (f"episode {e['n']} was refused for spend (ledger entry {waiting['ledger_seq']}) and the ceiling has not been raised since: "
                       "raise it with eprbench ledger raise to go on")  # fmt: skip
            break
        if budget is not None and not budget.can_start(e["model"], e["max_output_tokens"] or DEFAULT_MAX_OUTPUT):
            stopped = f"the spend budget cannot cover episode {e['n']}'s first request"
            break
        expected = {m: sorted(v)[0] for (_, m), v in versions.items() if len(v) == 1}
        context = {"plan": {"sha256": sha, "file": Path(path).name, "n": e["n"], "pair": e["pair"], "of": len(plan["episodes"])},
                   "expected_model_versions": expected} | ({"model_alias_allowed": True} if plan.get("allow_alias") else {})  # fmt: skip
        if accepted:
            context["agent_drift_accepted"] = {"reason": accept_drift, "drift": list(accepted)}
        token = RUN_CONTEXT.set(context | ({"unmetered_spend_accepted": True} if unmetered else {}))
        try:
            if e["provider"] == "agent":  # never a client: the agent CLI uses its own login
                report = run_agent(e, out_dir, **(agent_options or {}))
            else:
                report = run_one(e["task_id"], e["provider"], e["model"], e["interface"], client=clients(e["provider"]), effort=e["effort"], repeat=e["repeat"],
                                 limits=Limits(**e["limits"]), out_dir=out_dir, spend=budget, max_output_tokens=e["max_output_tokens"], unmetered=unmetered)  # fmt: skip
        finally:
            RUN_CONTEXT.reset(token)
        emit(report)
        ran += 1
        if report.get("terminal_reason") in COMPLETE_ENDINGS:
            finished += 1
        else:  # evidence kept, manifest incomplete: the episode runs again on resume
            errored[report.get("terminal_reason")] = errored.get(report.get("terminal_reason"), 0) + 1
        seen = versions.setdefault((e["provider"], e["model"]), set())
        seen.update(report.get("reported_models") or [])
        if stopped := _stop_after(report):
            break
        if len(seen) > 1:
            stopped = f"{e['model']} was answered by more than one model version: {sorted(seen)}"
            break
        if agents:
            facts.append((e, report))
            if stopped := drifted():
                break
    left = len(plan["episodes"]) - len(done) - finished
    return {"plan_sha256": sha, "episodes": len(plan["episodes"]), "already_complete": len(done), "ran": ran, "completed": finished, "errored": errored,
            "left": left, "stopped": stopped}  # fmt: skip
