import os
from pathlib import Path
import argparse

from eprbench.episode import Limits
from eprbench.runner import DEFAULT_MAX_OUTPUT, DEFAULT_MODELS, PINNED, SUPPORTED
from eprbench.tasks import build_world, list_tasks, load_task


def _settings(p) -> None:
    """The options that fix how an episode is run: shared by run and plan."""
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default="medium")
    p.add_argument("--max-requests", "--max-turns", dest="max_requests", type=int, default=Limits.max_requests,
                   help=f"stop after this many model requests (default {Limits.max_requests}); a run that reaches it is truncated")
    p.add_argument("--max-actions", type=int, default=Limits.max_actions,
                   help=f"stop after this many operations or mouse and keyboard actions (default {Limits.max_actions}); a run that reaches it is truncated")
    p.add_argument("--max-screenshots", type=int, default=Limits.max_screenshots,
                   help=f"browser only: screenshots and zooms the model may ask for, which are not actions (default {Limits.max_screenshots})")
    p.add_argument("--timeout", type=float, default=Limits.timeout_seconds, help=f"wall-clock seconds per episode (default {Limits.timeout_seconds:.0f})")
    p.add_argument("--no-final-chance", action="store_true", help="on reaching a limit, do not ask the model once more for its answer")
    p.add_argument("--final-chance-seconds", type=float, default=Limits.final_chance_seconds,
                   help=f"how long that last answer may take (default {Limits.final_chance_seconds:.0f}); a later one is not accepted")
    p.add_argument("--max-output-tokens", type=int,
                   help=f"the most output (reasoning included) one model response may have (Claude harnesses: default {DEFAULT_MAX_OUTPUT}; "
                        f"OpenAI: unbounded unless given, {DEFAULT_MAX_OUTPUT} under --max-spend or in a plan)")  # fmt: skip


def _limits(args) -> Limits:
    return Limits(max_requests=args.max_requests, max_actions=args.max_actions, max_screenshots=args.max_screenshots, timeout_seconds=args.timeout,
                  final_chance=not args.no_final_chance, final_chance_seconds=args.final_chance_seconds)  # fmt: skip


def _load_dotenv(path: str = ".env") -> None:
    """Put KEY=value lines from a local .env into the environment, without overriding what is already set."""
    env = Path(path)
    if not env.is_file():
        return
    for line in env.read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def main() -> None:
    _load_dotenv()
    parser = argparse.ArgumentParser(prog="eprbench")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("tasks", help="list available tasks")
    serve = sub.add_parser("serve", help="serve one task's world over HTTP")
    serve.add_argument("task")
    serve.add_argument("--mode", choices=["api", "ui", "both"], default="both")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--harness-token", help="enables /harness/reset and /harness/grade for whoever holds the token")
    serve.add_argument("--user", help="the logged-in clinician (defaults to the task's actor); the task is graded as theirs, so their actions earn the credit")
    serve.add_argument("--event-log", help="append one JSON line per request with wall-clock timing, for human baseline sessions")
    serve.add_argument("--out-dir", help="a person's session: keep the same evidence record as an agent's episode under this directory, "
                                         "ending when the reply page is submitted (or on Ctrl+C)")
    report = sub.add_parser("report", help="summarise run reports (JSON lines files) as tables")
    report.add_argument("files", nargs="+")
    report.add_argument("--trajectory", metavar="TASK", help="print the step-by-step trajectory of each run of this task instead")
    report.add_argument("--reviews", help="a clinician's reviews exported from the viewer: add the agreement table")
    view = sub.add_parser("view", help="build one self-contained HTML page for reading saved episodes")
    view.add_argument("dirs", nargs="+", help="evidence directories: an --out-dir, or episodes' own directories")
    view.add_argument("--out", default="episodes.html", help="the page to write (default episodes.html)")
    view.add_argument("--blind", action="store_true", help="replace model, provider, agent and harness names with stable codes; the key goes to a separate file")
    view.add_argument("--key", help="--blind: the key file to write or reuse (default: the page's name with .key.json)")
    view.add_argument("--public", action="store_true", help="leave out everything a published page must not carry (check definitions, ground truth)")
    view.add_argument("--embed", action="store_true", help="embed screenshots in the page instead of linking them relative to it")
    view.add_argument("--reviews", help="reviews exported from the page: load them into it and print the agreement table")
    view.add_argument("--include-review-comments", action="store_true", help="--public: keep reviewers' comments (sanitised) in the page")
    view.add_argument("--regrade", action="store_true", help="show each episode graded again with the current grader and packaged task (as eprbench regrade)")
    dmd = sub.add_parser("dmd-import", help="build a formulary catalogue from an NHS dm+d release (XML, from TRUD)")
    dmd.add_argument("release", help="folder containing the f_*.xml files")
    dmd.add_argument("out", help="catalogue to write, e.g. src/eprbench/data/catalogues/dmd.json")
    dmd.add_argument("--drug", action="append", help="keep only this drug (repeatable); default is everything")
    dmd.add_argument("--source", help="release label recorded in the catalogue, e.g. '10.2.0, 19 October 2026'")
    lint = sub.add_parser("lint", help="check a packaged bundle for mistakes the schema cannot catch")
    lint.add_argument("bundle")
    run = sub.add_parser("run", help="run a model or agent CLI on tasks and print one JSON report per line")
    run.add_argument("tasks", nargs="*", help="task ids, or 'all' (not with --plan)")
    run.add_argument("--plan", help="run the episodes of a plan written by eprbench plan, in its order, skipping those already complete under --out-dir")
    run.add_argument("--interface", choices=["tool", "ui", "api"], help="tool (default) or ui for SDK harnesses; api (default) or ui for --agent")
    run.add_argument("--agent", help="run a logged-in agent CLI instead (claude, codex), over the JSON API or, with --interface ui, the web UI from "
                                     "screenshots; needs no API key")
    run.add_argument("--model", default=None, help="defaults: claude-opus-5-5, gemini-3.8-flash, gpt-5.4-mini")
    run.add_argument("--provider", choices=["anthropic", "gemini", "openai"], default="anthropic")
    run.add_argument("--repeats", type=int, default=1)
    _settings(run)
    run.add_argument("--out-dir", help="write a complete evidence record of every episode under this directory")
    run.add_argument("--ledger", help="SDK runs (required): the spend ledger made by eprbench ledger init, whose ceiling holds across every "
                                      "invocation and process using it; every request is reserved in it before it is sent (docs/SPEND_SAFETY.md)")
    run.add_argument("--prices", help="SDK runs (required): the prices file (JSON; see docs/prices.example.json). No price is built in")
    run.add_argument("--max-spend", type=float, metavar="GBP", help="also a lower ceiling for this invocation alone")
    run.add_argument("--max-run-spend", type=float, metavar="GBP", help="also a ceiling for each episode")
    run.add_argument("--spend-reserve", type=float, default=0.0, metavar="GBP",
                     help="keep this much of the ledger's ceiling for final chances, which alone may spend it (default 0)")
    run.add_argument("--max-request-bytes", type=int, metavar="BYTES", help="refuse to send a larger request, images included (default 20,000,000)")
    run.add_argument("--allow-alias", action="store_true",
                     help="SDK runs: accept a model id that is not a dated snapshot (a provider may change what it means); recorded, and the "
                          "model version each response names is checked against the first")  # fmt: skip
    run.add_argument("--new-ledger-for-existing-run", metavar="REASON",
                     help="--plan: spend against a ledger other than the one the plan or the evidence under --out-dir names (the old one was "
                          "lost): refused without this, and the reason is recorded in the ledger")
    run.add_argument("--i-accept-unmetered-spend", dest="unmetered", action="store_true",
                     help="run paid SDK episodes with no ledger, no prices and no ceiling: nothing limits what they cost (recorded in each episode)")
    run.add_argument("--allow-unsandboxed", action="store_true",
                     help="--agent only: run the agent CLI even where it cannot be sandboxed away from the ground truth (such runs are reported apart)")
    run.add_argument("--protect", action="append", default=[], help="--agent only: another path the agent must not read (repeatable)")
    run.add_argument("--agent-model", help="--agent only: the model the agent CLI should use (default: the CLI's own)")
    run.add_argument("--agent-effort", help="--agent only: the reasoning effort the agent CLI should use (default: the CLI's own)")
    run.add_argument("--accept-agent-drift", metavar="REASON",
                     help="--plan of agent CLIs: go on although a CLI version or the model it reports has changed since the plan began (recorded)")
    run.add_argument("--agent-api-keys", action="store_true",
                     help="--agent only: let the agent CLI see provider API keys from the environment, so it may bill an API account "
                          "rather than use its subscription login")
    plan = sub.add_parser("plan", help="write a seeded, counterbalanced run order for a paired comparison of interfaces (run it with run --plan)")
    plan.add_argument("tasks", nargs="+", help="task ids, or 'all'")
    plan.add_argument("--interfaces", nargs="+", choices=["tool", "ui", "api"], help="default: tool ui (--models) or api ui (--agents)")
    who = plan.add_mutually_exclusive_group(required=True)
    who.add_argument("--models", nargs="+", metavar="PROVIDER:MODEL", help="bare models through the SDK harnesses, e.g. anthropic:claude-haiku-5-5 openai:gpt-5.4-mini")
    who.add_argument("--agents", nargs="+", metavar="CLI[:MODEL]", help="logged-in agent CLIs (claude, codex), each with the model to pin or its own default; "
                                                                        "subscription runs: no --prices or --ledger")  # fmt: skip
    plan.add_argument("--agent-effort", help="--agents: the reasoning effort each agent CLI is told (default: its own)")
    plan.add_argument("--repeats", type=int, default=2, help="pairs per task and model (default 2)")
    plan.add_argument("--seed", type=int, required=True)
    plan.add_argument("--out", required=True, help="the plan file to write (JSON)")
    _settings(plan)
    plan.add_argument("--prices", help="--models (required): the prices file the plan will be run with: its SHA-256 is pinned in the plan")
    plan.add_argument("--ledger", help="--models (required): the ledger the plan will spend against (eprbench ledger init): its id is pinned in the plan")
    plan.add_argument("--allow-alias", action="store_true", help="accept model ids that are not dated snapshots (a provider may change what they mean); recorded")
    ledger = sub.add_parser("ledger", help="the spend ledger every paid run is reserved against (docs/SPEND_SAFETY.md)")
    ledger_sub = ledger.add_subparsers(dest="ledger_command", required=True)
    ledger_init = ledger_sub.add_parser("init", help="create a ledger with its ceiling (once; it never overwrites)")
    ledger_init.add_argument("file")
    ledger_init.add_argument("--ceiling", type=float, required=True, metavar="GBP")
    ledger_init.add_argument("--replace-reason", help="a ledger was at this path before: make a new one there anyway, for this reason (recorded)")
    ledger_raise = ledger_sub.add_parser("raise", help="raise a ledger's ceiling, with the reason, as an entry in the ledger")
    ledger_raise.add_argument("file")
    ledger_raise.add_argument("--ceiling", type=float, required=True, metavar="GBP")
    ledger_raise.add_argument("--reason", required=True)
    ledger_show = ledger_sub.add_parser("show", help="validate a ledger and print its ceiling and what has been spent")
    ledger_show.add_argument("file")
    ledger_adopt = ledger_sub.add_parser("adopt", help="anchor a ledger that has entries but no anchor here (moved, or its anchors lost); recorded")
    ledger_adopt.add_argument("file")
    ledger_adopt.add_argument("--reason", required=True)
    ledger_estimate = ledger_sub.add_parser("estimate", help="what a plan's requests reserve, and how many the ceiling admits")
    ledger_estimate.add_argument("--plan", required=True)
    ledger_estimate.add_argument("--prices", required=True)
    ledger_estimate.add_argument("--ledger", help="the ledger whose ceiling (less what it has spent) to use")
    ledger_estimate.add_argument("--ceiling", type=float, metavar="GBP", help="or a ceiling to use")
    ledger_estimate.add_argument("--from-evidence", metavar="DIR", help="earlier runs' evidence: add the expected case from their measured usage")
    ledger_estimate.add_argument("--growth-bytes", type=int, metavar="BYTES", help="what each request adds to the next (default: the maximum output at 4 bytes a token, plus 8,000)")
    regrade = sub.add_parser("regrade", help="grade saved episodes again from their final worlds with the current grader")
    regrade.add_argument("dir", help="an --out-dir, or one episode's directory")
    regrade.add_argument("--saved-task", action="store_true", help="use each episode's saved task definition rather than the current one")
    args = parser.parse_args()

    if args.command == "tasks":
        for task_id in list_tasks():
            task = load_task(task_id)
            print(f"{task.id}\t{task.category}\t{task.title}")
        return

    if args.command == "report":
        from eprbench import report as reporting

        reports = reporting.load_reports(args.files)
        if args.trajectory:
            print("\n\n".join(reporting.trajectory(r) for r in reports if r["task_id"] == args.trajectory))
        else:
            print(reporting.render(reports))
            if args.reviews:
                print(f"## Clinician review\n\n{reporting.agreement_table(reporting.load_reviews(args.reviews), reports)}")
        return

    if args.command == "view":
        from eprbench import report as reporting
        from eprbench import viewer

        built = viewer.build(args.dirs, args.out, blind=args.blind, public=args.public, embed=args.embed, reviews=args.reviews, key=args.key,
                             include_review_comments=args.include_review_comments, regrade=args.regrade)
        print(f"{built['episodes']} episodes written to {built['out']}" + (f"; key in {built['key']}" if built.get("key") else ""))
        if args.reviews:
            print(reporting.agreement_table(reporting.load_reviews(args.reviews), built["rows"]))
        return

    if args.command == "dmd-import":
        from eprbench.dmd import write_catalogue

        print(f"{write_catalogue(args.release, args.out, args.drug, args.source)} products written to {args.out}")
        return

    if args.command == "lint":
        from eprbench.lint import lint_bundle
        from eprbench.loader import read_bundle

        problems = lint_bundle(read_bundle(args.bundle))
        print("\n".join(problems) or "clean")
        raise SystemExit(1 if problems else 0)

    if args.command == "regrade":
        import json

        from eprbench.episode import episode_dirs, regrade

        dirs = episode_dirs(args.dir)
        if not dirs:
            parser.error(f"no saved episodes under {args.dir}")
        print("| Episode | Task | Terminal reason | Old score | New score | Old tier | New tier | Changed | Grader changed | Task changed "
              "| Graded world reproduced | Horizon world reproduced |")
        print("| --- |" + " --- |" * 11)
        yes = lambda x: "-" if x is None else "yes" if x else "no"  # noqa: E731
        fmt = lambda x: "-" if x is None else f"{x:.2f}"  # noqa: E731
        for d in dirs:
            r = regrade(d, args.saved_task)
            changed = (r["old_score"], r["old_success"], r["old_unsafe"], r["old_tier"]) != (r["new_score"], r["new_success"], r["new_unsafe"], r["new_tier"])
            print(f"| {r['episode']} | {r['task_id']} | {r['terminal_reason']} | {fmt(r['old_score'])} | {fmt(r['new_score'])} | {r['old_tier'] or '-'} "
                  f"| {r['new_tier']} | {yes(changed)} | {yes(r['grader_changed'])} | {yes(r['task_changed'])} | {yes(r['graded_world_reproduced'])} "
                  f"| {yes(r['horizon_world_reproduced'])}{f' (migrated from schema {m})' if (m := r['world_migrated_from']) else ''} |")
        return

    if args.command == "ledger":
        import json

        from eprbench.spend import Ledger, SpendError

        if args.ledger_command == "estimate":
            from eprbench.runner import estimate, load_plan
            from eprbench.spend import Prices

            try:
                plan, _ = load_plan(args.plan)
                if args.ledger:
                    state = Ledger(args.ledger).read()
                    ceiling = state.ceiling - state.spent()
                elif args.ceiling:
                    ceiling = args.ceiling
                else:
                    raise SpendError("give --ledger or --ceiling")
                print(json.dumps(estimate(plan, Prices(args.prices), ceiling, args.from_evidence, args.growth_bytes), indent=1))
            except (SpendError, ValueError, OSError) as e:
                parser.error(str(e))
            return
        try:
            if args.ledger_command == "adopt":
                state = Ledger.adopt(args.file, args.reason).read()
            elif args.ledger_command == "init":
                state = Ledger.init(args.file, args.ceiling, replace_reason=args.replace_reason).read()
            elif args.ledger_command == "raise":
                Ledger(args.file).raise_ceiling(args.ceiling, args.reason)
                state = Ledger(args.file).read()
            else:
                state = Ledger(args.file).read()
        except SpendError as e:
            parser.error(str(e))
        print(json.dumps({"ledger": args.file, "ledger_id": state.ledger_id, "ceiling_gbp": state.ceiling, "spent_gbp": round(state.spent(), 6),
                          "reservations": state.requests(), "unreconciled": len(state.outstanding()), "entries": state.seq}))  # fmt: skip
        return

    if args.command == "plan":
        import json

        from eprbench.runner import make_plan
        from eprbench.spend import Prices, SpendError

        if args.agents:  # agent CLIs: their subscriptions, no money, no client
            if args.prices or args.ledger:
                parser.error("an agent CLI uses its subscription and is not metered here: give no --prices or --ledger")
            agents = [(a.split(":", 1)[0], a.split(":", 1)[1] if ":" in a else None) for a in args.agents]
            try:
                made = make_plan(args.tasks, args.interfaces or ["api", "ui"], [], args.repeats, args.seed, effort=args.agent_effort, limits=_limits(args),
                                 max_output_tokens=None, agents=agents)  # fmt: skip
            except (ValueError, FileNotFoundError) as e:
                parser.error(str(e))
            Path(args.out).write_text(json.dumps(made, indent=1) + "\n")
            print(f"{len(made['episodes'])} episodes in {len(made['pairs'])} pairs written to {args.out}")
            return
        if args.agent_effort:
            parser.error("--agent-effort applies to --agents plans")
        if not (args.prices and args.ledger):
            parser.error("a plan of paid SDK runs needs --prices and --ledger (see docs/SPEND_SAFETY.md)")
        models = [tuple(m.split(":", 1)) for m in args.models]
        if any(len(m) != 2 or m[0] not in DEFAULT_MODELS or not m[1] for m in models):
            parser.error("--models takes PROVIDER:MODEL, the provider one of " + ", ".join(sorted(DEFAULT_MODELS)))
        from eprbench.spend import Ledger

        try:
            ledger_id = Ledger(args.ledger).read().ledger_id
            prices = Prices(args.prices)
            for provider, model in models:
                prices.model(model)
            made = make_plan(args.tasks, args.interfaces or ["tool", "ui"], models, args.repeats, args.seed, effort=args.effort, limits=_limits(args),
                             max_output_tokens=args.max_output_tokens or DEFAULT_MAX_OUTPUT, prices_sha256=prices.sha256, ledger_id=ledger_id,
                             allow_alias=args.allow_alias)  # fmt: skip
        except (ValueError, FileNotFoundError, SpendError) as e:
            parser.error(str(e))
        Path(args.out).write_text(json.dumps(made, indent=1) + "\n")
        print(f"{len(made['episodes'])} episodes in {len(made['pairs'])} pairs written to {args.out}")
        return

    if args.command == "run":
        import json
        import sys

        from eprbench.episode import RUN_CONTEXT
        from eprbench.runner import Clients, run_one
        from eprbench.spend import MAX_REQUEST_BYTES, Budget, Ledger, Prices, SpendError

        interface = args.interface or ("api" if args.agent else "tool")
        agent_plan = False
        if args.plan:
            from eprbench.runner import is_agent_plan, load_plan

            try:
                agent_plan = is_agent_plan(load_plan(args.plan)[0])
            except (ValueError, OSError) as e:
                parser.error(str(e))
        elif not args.tasks:  # what can never run is refused first, whatever the spend options
            parser.error("give task ids (or 'all'), or --plan")
        else:
            if args.agent and interface not in ("api", "ui"):
                parser.error("--agent runs over the JSON API (--interface api) or the web UI (--interface ui)")
            if not args.agent and (args.provider, interface) not in SUPPORTED:
                parser.error(f"--provider {args.provider} does not support --interface {interface}; supported: "
                             + ", ".join(f"{p}/{i}" for p, i in sorted(SUPPORTED)))
        if not (args.agent or agent_plan) and (args.allow_unsandboxed or args.protect or args.agent_model or args.agent_effort or args.agent_api_keys):
            parser.error("--allow-unsandboxed, --protect and the --agent-* options apply to agent CLI runs only")
        if agent_plan and (args.agent_model or args.agent_effort):
            parser.error("--plan takes the agents' models and effort from the plan")
        spend_flags = [f for f, v in (("--ledger", args.ledger), ("--prices", args.prices), ("--max-spend", args.max_spend), ("--max-run-spend", args.max_run_spend),
                                      ("--spend-reserve", args.spend_reserve or None), ("--max-request-bytes", args.max_request_bytes)) if v is not None]  # fmt: skip
        if (args.agent or agent_plan) and (spend_flags or args.unmetered):
            parser.error("an agent CLI uses its subscription and is not metered here: spend options apply to SDK runs")
        budget = None
        if not (args.agent or agent_plan):
            # Every paid SDK episode is metered, whatever the provider, harness or plan: checked here, before any client
            # exists. The only way past it is the explicit, recorded acceptance of unmetered spend.
            if args.unmetered:
                if spend_flags:
                    parser.error("--i-accept-unmetered-spend runs without a ledger: give no spend options with it")
                print("WARNING: --i-accept-unmetered-spend: these paid runs have no ledger, no prices and no ceiling; nothing limits what they cost.",
                      file=sys.stderr)  # fmt: skip
            else:
                if not (args.ledger and args.prices):
                    parser.error("a paid SDK run needs --ledger (made once by eprbench ledger init FILE --ceiling GBP) and --prices; "
                                 "see docs/SPEND_SAFETY.md (or --i-accept-unmetered-spend)")  # fmt: skip
                try:
                    budget = Budget(Prices(args.prices), Ledger(args.ledger), invocation_gbp=args.max_spend, per_run_gbp=args.max_run_spend,
                                    reserve_gbp=args.spend_reserve, max_request_bytes=args.max_request_bytes or MAX_REQUEST_BYTES)  # fmt: skip
                except (SpendError, OSError) as e:
                    parser.error(str(e))
        context = RUN_CONTEXT.set({"unmetered_spend_accepted": True}) if args.unmetered else None
        if args.plan:
            from eprbench.runner import load_plan, pin_changes, run_plan

            if args.tasks or args.agent:
                parser.error("--plan takes its tasks, models and settings from the plan: give no tasks or --agent")
            if not args.out_dir:
                parser.error("--plan needs --out-dir: it resumes by skipping episodes whose evidence there is complete")
            from eprbench.runner import approve_replacement, ledger_problems

            try:
                plan, sha = load_plan(args.plan)
                for e in plan["episodes"] if budget is not None else []:
                    if e["provider"] == "gemini":
                        raise ValueError("the Gemini harness has no bounded output, so no request of it can be priced in advance")
                    budget.prices.model(e["model"])
                if changed := pin_changes(plan, budget.prices.sha256 if budget is not None else None):
                    raise ValueError("the plan no longer matches what it was written for: " + "; ".join(changed))
                if budget is not None:
                    state = budget.ledger.read()
                    problems = ledger_problems(plan, sha, args.out_dir, state)
                    if problems and not args.new_ledger_for_existing_run:
                        raise ValueError("this is not the ledger the plan and its evidence spent against: " + "; ".join(m for _, m in problems)
                                         + ". If the old ledger is lost, --new-ledger-for-existing-run REASON (recorded)")  # fmt: skip
                    if problems:
                        approve_replacement(budget.ledger, sha, problems, args.new_ledger_for_existing_run)
            except (ValueError, OSError, SpendError) as e:
                parser.error(str(e))
            options = {"allow_unsandboxed": args.allow_unsandboxed, "protect": args.protect, "pass_credentials": args.agent_api_keys} if agent_plan else None
            if args.accept_agent_drift and not agent_plan:
                parser.error("--accept-agent-drift applies to plans of agent CLIs")
            done = run_plan(args.plan, args.out_dir, None if agent_plan else Clients(), lambda r: print(json.dumps(r, default=str), flush=True), budget,
                            unmetered=args.unmetered, agent_options=options, accept_drift=args.accept_agent_drift)  # fmt: skip
            print(json.dumps({"plan": done}), file=sys.stderr)
            if done["stopped"] or done["left"]:
                why = f"Stopped: {done['stopped']}; " if done["stopped"] else ""
                errored = ", ".join(f"{n} {reason}" for reason, n in sorted(done["errored"].items()))
                print(f"{why}{done['left']} episodes left{f' ({errored} this run)' if errored else ''}. Run the same command again to resume.", file=sys.stderr)
                raise SystemExit(3)
            return
        limits = _limits(args)
        task_ids = list_tasks() if args.tasks == ["all"] else args.tasks
        model = args.model or DEFAULT_MODELS[args.provider]
        if not args.agent and not PINNED.search(model) and not args.allow_alias:
            parser.error(f"{model} is not a dated snapshot, so the provider may change what it means: --allow-alias to run it anyway (recorded)")
        base_context = dict(RUN_CONTEXT.get()) | ({"model_alias_allowed": True} if args.allow_alias and not PINNED.search(model) else {})
        versions: set[str] = set()
        output = args.max_output_tokens or (DEFAULT_MAX_OUTPUT if budget is not None else None)
        if budget is not None:
            if args.provider == "gemini":
                parser.error("the Gemini harness has no bounded output, so no request of it can be priced in advance (or --i-accept-unmetered-spend)")
            try:
                budget.prices.model(model)  # refused before anything is spent
            except SpendError as e:
                parser.error(str(e))
        client = None if args.agent else Clients()(args.provider)  # each SDK reads its own API key
        common = {"limits": limits, "out_dir": args.out_dir}
        for task_id in task_ids:
            for repeat in range(args.repeats):
                if args.agent:
                    from eprbench.agents import cli_agent, cli_browser

                    harness = cli_browser if interface == "ui" else cli_agent  # the web UI from screenshots, or the JSON API
                    report = harness.run_episode(task_id, args.agent, allow_unsandboxed=args.allow_unsandboxed, protect=args.protect, model=args.agent_model,
                                                 effort=args.agent_effort, pass_credentials=args.agent_api_keys, repeat=repeat, **common)  # fmt: skip
                else:
                    if budget is not None and not budget.can_start(model, output or DEFAULT_MAX_OUTPUT):
                        print("Stopped: the spend budget cannot cover another episode's first request.", file=sys.stderr)
                        raise SystemExit(3)
                    expected = {model: sorted(versions)[0]} if len(versions) == 1 else {}
                    token = RUN_CONTEXT.set(base_context | ({"expected_model_versions": expected} if expected else {}))
                    try:
                        report = run_one(task_id, args.provider, model, interface, client=client, effort=args.effort, repeat=repeat, spend=budget,
                                         max_output_tokens=output, unmetered=args.unmetered, **common)  # fmt: skip
                    finally:
                        RUN_CONTEXT.reset(token)
                    versions.update(report.get("reported_models") or [])
                print(json.dumps(report, default=str), flush=True)
                if drift := report.get("model_drift"):  # the provider changed the model behind the id: nothing more runs
                    print(f"Stopped: {drift['model']} was answered by {drift['reported']}, not {drift['expected']}.", file=sys.stderr)
                    raise SystemExit(3)
                if report.get("terminal_reason") == "spend_limit":  # nothing more runs once the budget has refused a request
                    print("Stopped: a request was refused for spend; raise the ceiling with eprbench ledger raise to go on.", file=sys.stderr)
                    raise SystemExit(3)
        return

    import uvicorn

    from eprbench.server.app import create_app

    task = load_task(args.task)
    if args.user:  # the person at the keyboard is the task's actor, so the checks that credit the actor credit them
        task = task.model_copy(update={"actor": args.user})
    print(f"Task: {task.instruction}")
    if args.out_dir:
        import json

        from eprbench.server.local import human_session

        if args.harness_token:
            parser.error("--harness-token is for agent harnesses, not a person's session (--out-dir)")
        ready = lambda url, app: print(f"Serving {url} for {task.actor}; submit the reply page (Handover / reply) to finish.", flush=True)  # noqa: E731
        print(json.dumps(human_session(task, args.mode, args.out_dir, args.host, args.port, args.event_log, on_ready=ready), default=str))
        return
    app = create_app(lambda: build_world(task), mode=args.mode, actor=task.actor, task=task, harness_token=args.harness_token, event_log=args.event_log)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
