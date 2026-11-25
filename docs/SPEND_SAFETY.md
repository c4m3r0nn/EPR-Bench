# Spend safety: what the ceiling guarantees

Every paid request an SDK harness makes is reserved in a ledger before it is sent (`src/eprbench/spend.py`). This
page says what the code enforces, under which assumptions, and what it does not. Nothing here authorises spending: a
paid run needs the owner's explicit agreement first.

## First: a provider-side hard limit (the operator's job)

The ledger is the **second** line of defence. The first is a limit the provider enforces, which this code cannot
check and the operator must set **before any paid run**, for every provider used:

- a dedicated API key in its own workspace or project, funded with prepaid credit equal to the ceiling, with
  automatic top-up (auto-reload) turned off; or
- a provider project budget with a hard cap that stops requests (not a notification threshold).

Prepaid credit is a hard stop only if that provider actually refuses requests once the credit is spent, rather than
letting the balance go negative or billing the overage later. **Verify this for the provider in question, from its
current billing documentation or support, before relying on it; do not assume it.** Only then can a fault in this
code, its prices file or its assumptions cost no more than the provider-side limit allows.

The operator's order of work:

1. Set and verify the provider's actual hard cap; turn automatic reload off.
2. Use one machine, one fixed state directory, one physical ledger and one runner at a time.
3. Check the prices and the allowances against the provider's documentation; initialise the ledger at £10; create a
   small paired plan.
4. Read the estimate (`ledger estimate`) cautiously; run metered, with no overrides and no unmetered options.
5. Inspect the usage, the reported model versions, any refusals and the evidence before expanding the plan.

## How a paid run is set up

```sh
uv run eprbench ledger init results/preview/ledger.jsonl --ceiling 10      # once; never overwrites
uv run eprbench plan ... --prices ~/prices.json --ledger results/preview/ledger.jsonl --out plan.json
uv run eprbench ledger estimate --plan plan.json --prices ~/prices.json --ledger results/preview/ledger.jsonl [--from-evidence DIR]
uv run eprbench run --plan plan.json --out-dir results/preview --ledger results/preview/ledger.jsonl --prices ~/prices.json
uv run eprbench ledger show results/preview/ledger.jsonl
uv run eprbench ledger raise results/preview/ledger.jsonl --ceiling 12 --reason "owner approved two more pairs"
```

- **The estimate.** `ledger estimate` prints, per model and interface, figures that are estimates under stated
  assumptions, not promises:
  - the reservation of an episode's first request, and the largest a single request can reserve;
  - an upper bound on what one episode reserves if every reservation is kept. It assumes the plan's request limit
    (plus three final-chance requests), and that each request carries everything before it, growing by a stated
    number of bytes a round (by default, the maximum output at 4 bytes a token plus 8,000 bytes; `--growth-bytes`)
    and, in the browser, one more screenshot a round, capped at the largest request. It prints those assumptions, how
    many of one episode's requests fit within the ceiling, and how many such episodes would. A conversation that grows
    faster than assumed reserves more, up to the per-request size limit;
  - with `--from-evidence`, the mean measured cost per episode of earlier runs, at these prices. Episodes with any
    response lacking usable usage are counted apart ("without usable usage") and never priced as zero.
- **Metering is required.** No paid SDK run, plan or not, any provider, starts without `--ledger` and `--prices`:
  the command refuses before any client is made. Each SDK harness also refuses a client without a meter unless it is
  called with `unmetered=True`. The CLI passes that only under `--i-accept-unmetered-spend`, which prints a warning.
  The tests pass it through a helper that marks scripted stand-in clients. How each episode was metered is recorded
  in it (`metering`).
- **Gemini** is refused with a ledger, because its harness sends no output bound.
- **Model ids must be dated snapshots** (`...-20250929`, `...-2025-08-07`) in plans and direct runs, unless
  `--allow-alias` is given. That is recorded in the episode. This project's Claude ids, such as `claude-haiku-5-5`,
  have no date, so they need `--allow-alias`. Each response's model version is recorded and checked against the
  first one reported for that id, and a change stops the run. A response naming no version marks the episode
  `unverified`.
- **Agent-CLI runs** (`--agent`) use the CLI's subscription and are not metered here.

## What the code enforces

Let *C* be the ledger's ceiling. It is raised only by a `raise` entry in the ledger itself. Let *S* be the sum, over
every reservation in the ledger, of one of two amounts:

- its actual cost, if its response came back with usage that passes the checks below (*confirmed*);
- otherwise its whole reserved amount (*kept*). This covers an attempt that failed or timed out, a response whose
  usage is missing or fails the checks, and a process that stopped first.

**Under the assumptions below, S never exceeds C, and every request sent through these harnesses was reserved in the
ledger first.**

### Reserve, then send

A request is sent only after its reservation has been appended to the ledger, flushed and fsynced. The steps, in
order:

1. Take an exclusive `fcntl` lock on the opened ledger file. Check that the file at the path is still the one locked
   (same device and inode).
2. Read the whole ledger and validate it. Take a second exclusive lock, on the ledger's id in the state directory
   (one per ledger id on this machine), and check the ledger against its anchors inside it (below).
3. Check that an anchor can be written, before anything is appended.
4. Append the reservation, but only if S plus the request's bound stays within C, less the `--spend-reserve` (which
   only a final chance may use), and within any `--max-spend` and `--max-run-spend`.
5. Update the anchor.

Each retry is a new attempt and is reserved again.

### Usage checks (finding C)

A reservation is replaced by an actual cost only if all of these hold:

- every token class the provider bills is reported, as a whole non-negative number (Claude: input, cache writes, cache
  reads, output; OpenAI: input, cached input, output);
- the input is above zero;
- for a request continuing an earlier response by reference, the input is at least the input of the last response
  whose usage was confirmed.

These are the only checks: presence, whole non-negative numbers, some input, and continuity with the last confirmed
input. There is no tokens-per-byte floor, since no tokeniser guarantees one. If a check ever rejects usage that was
in fact correct, the reservation is kept in full: that uses up ceiling, not money. A response without usable usage keeps its reservation, but its upper bound is never used as a floor
for later responses.

### The bound on one request

- **Input:** its serialised text in UTF-8 bytes, plus `max_image_tokens` for each image, plus
  `request_overhead_tokens`, plus (for a continued response) that response's reported input and output. All priced
  at the dearest input rate (uncached, cache write or cache read).
- **What counts as an image:** a structurally complete PNG (signature, IHDR first, IDAT, IEND last, every chunk's CRC
  right) no larger than 1280 x 800, in an image field of the requests the harnesses send. The fields are an Anthropic
  image block in a user message or a tool result, and an OpenAI `input_image` or computer screenshot. Everything else
  is counted by its bytes.
- **Output:** the request's maximum output at the output rate. A request with no output bound is never sent.

### Backstops that do not depend on that estimate

- A ledger's requests can number at most C divided by the cheapest a request can be.
- Its reserved output tokens can total at most C divided by the output rate.
- No request is sent whose serialised size, images included, is over `--max-request-bytes` (default 20,000,000).

### Anchors

The state directory is `$EPRBENCH_STATE_DIR`, else `$XDG_STATE_HOME/eprbench`, else `~/.local/state/eprbench`. Its
files are mode 0600 and its directories 0700. It records, for each ledger id, the last entry written (sequence number
and hash), and for each path, the ledger id there.

| Situation, on this machine with this state directory | What happens |
| --- | --- |
| A ledger behind its anchor (an earlier copy restored) | Refused |
| A ledger whose history does not continue its anchor | Refused |
| A different ledger at a path whose ledger is known (a file replaced) | Refused |
| A ledger with entries but no anchor | Refused unless `eprbench ledger adopt FILE --reason TEXT` (recorded) |
| A new ledger where one was | Refused unless `ledger init --replace-reason` (recorded) |
| An unwritable anchor directory | Stops before anything is appended |

`ledger adopt` covers a ledger moved here, or whose anchors were lost. The operator then vouches that it is the
latest copy.

### Plans and evidence

- A plan names its ledger's id, and the ledger records each plan it serves.
- Each episode's manifest records the ledger id, the range of its entries and the hash of the last one.
- `run --plan` refuses a ledger that is not the plan's, or not the one a manifest under `--out-dir` names, unless
  `--new-ledger-for-existing-run REASON` is given.
- That override is recorded, and approves exactly that replacement: those old ledger ids, for that plan, by this
  ledger. A later mismatch, such as a further replacement, is refused again.
- A ledger that a manifest shows had got further, or had other entries, is refused, and no override approves that.

### Refusals

A request refused for spend, at any request site (a final chance included), ends its episode at `spend_limit`. That
stops `eprbench run` and `run --plan` with exit code 3. The refused episode keeps its evidence, and its manifest says
`complete: false`, so a resumed plan runs it again. It does so only after a `ledger raise` later than the episode's
last ledger entry.

### Validation

A ledger that does not validate stops every run until it is repaired. That covers a broken hash chain, a sequence gap,
an unknown kind, a non-finite or negative amount, NaN, or a cut-off last line. A cut-off last line is a write
interrupted part-way. Deleting that one line is always safe: a reservation is complete before its request is sent,
and one without its reconciliation counts in full.

## The assumptions

1. **The prices file is right.** Every rate is at least the provider's real rate for that model, for every token
   class. Prices in another currency carry a conservative rate into pounds, raised by `fx_safety_margin`.
2. **`max_image_tokens` is at least what a 1280 x 800 PNG costs on that model, and `request_overhead_tokens` at least
   what the provider adds to every request beyond what is sent**, from the provider's documentation.
3. **No request carries more input tokens than its serialised text has UTF-8 bytes, plus the allowances above.** A
   token covers at least one byte of the text it encodes. The JSON keys, quotes and brackets counted with the text
   stand in for the few tokens a provider uses to mark out messages and blocks. The providers' token-counting
   endpoints are not used: neither installed SDK documents them as free of charge.
4. **A provider bills no more output than the request's bound** (reasoning included), **and bills according to the
   usage it reports**, whenever that usage passes the checks above.
5. **Every paid request goes through these harnesses with a meter on this ledger**, on a local filesystem where
   `fcntl` locks work (not a network share). The SDKs' own retries are off (`agents.without_retries`).
6. **One machine, one state directory, one copy of the ledger.**
7. **Nobody forges the ledger and its anchors together.** The hash chain and the anchors detect corruption, rollback
   and accidental edits, not deliberate forgery by someone with write access to both.

## What is not guaranteed

Cases the code does not catch:

- **Deleting the ledger and its anchors together**, then starting a fresh ledger at a new path, or with a new plan and
  a new out-dir. The fresh ledger has a fresh ceiling. If the old plan or its evidence survive, `run --plan` refuses
  the fresh ledger without a recorded override.
- **A copied ledger, with the same id, used from another state directory** (another machine, another user, a changed
  `EPRBENCH_STATE_DIR`). Each copy has its own lock and its own anchors there, so each can spend up to the ceiling.
  On one machine with one state directory, every change to a ledger also takes a lock on its id there, beside its
  anchor, and checks the anchor again inside it. So two copies at different paths, even used at the same moment,
  cannot both append: once one has, the other is behind the anchor and is refused (tested with two processes).
- **A different machine has no anchors.** A ledger moved there is refused until it is adopted. After that, it is
  protected there from then on, but nothing there knows what happened to its other copies.

Things outside the code's reach:

- Charges the provider makes that are not in the usage it reports: minimum charges, rounding, taxes, fees for
  provider-side tools, storage, a price change on the day.
- Usage a provider under-reports while still passing the checks above.
- Any error in the prices file or the allowances. If the file understates the real prices by a factor *k*, the bill
  can reach *k* x C.
- Requests made outside these harnesses, under `--i-accept-unmetered-spend` or `unmetered=True`, after an override,
  and agent-CLI runs.
- That the ceiling is spent well. A request without usable usage keeps its whole reservation, and a run stops when
  its next request *could* cross the ceiling, usually well below the actual bill. Run `ledger estimate` before
  committing to a plan.

## Worst-case overshoot for a 24-episode plan

Under assumptions 1 to 7, **nothing over the ceiling: £0.00**, apart from binary floating-point rounding in the sums,
of the order of 10^-15 of the ceiling. With a £10 ledger, the total billed for all 24 episodes is at most £10. That
holds through any number of retries and failed attempts, responses without usable usage, crashes and interrupts,
resumes, and invocations sharing the ledger on that machine. Each attempt is reserved before it is sent, kept in full
unless its usage passes the checks, and admitted only within the ceiling as the ledger stands under its lock.

There is no bound in three cases: assumption 1 broken by a factor *k* (the bill can reach £10 x *k*), assumption 6
broken (copies of the ledger used from several state directories, or the ledger and its anchors deleted and started
afresh), or a run outside the meter. The provider-side hard limit is what bounds the bill in those cases. Set it first.
