"""A ceiling in pounds on paid (SDK) runs: reserve before every request, reconcile after, in one locked ledger.

docs/SPEND_SAFETY.md states what this guarantees and under which assumptions. In short: every paid request is preceded
by a durable reservation of its most possible cost, made under an exclusive lock on the ledger after re-reading and
validating all of it, and refused if it would take the ledger's total over its ceiling; the reservation is replaced by
the actual cost only when a response with its usage has come back, and is otherwise kept as spent.

THE PRICES FILE (`--prices FILE`; docs/prices.example.json has the shape, with fake numbers). Nothing is built in or
defaulted. Per model id: the price per million tokens of every token class, each strictly positive; the most tokens
one image can cost on that model (`max_image_tokens`); and the tokens the provider adds to every request beyond what is
sent, such as a tool or computer-use system prompt (`request_overhead_tokens`). Prices in pounds have `gbp_per_unit`
exactly 1. In any other currency (a three-letter code) `gbp_per_unit` is a conservative rate into pounds between 0.1
and 10, and `fx_safety_margin` (above 0, at most 1) raises it further: costs are converted at rate x (1 + margin).

    {"currency": "USD", "gbp_per_unit": 0.8, "fx_safety_margin": 0.1,
     "models": {"<model id>": {"per_million_tokens": {"input": 1.0, "cache_write": 1.25, "cache_read": 0.1, "output": 5.0},
                               "max_image_tokens": 1600, "request_overhead_tokens": 1000}}}

THE BOUND ON A REQUEST. Input tokens are bounded by the request's serialised text in UTF-8 bytes (no tokeniser emits
more tokens than the bytes of the text it encodes, since every token covers at least one byte; the JSON keys, quotes
and brackets counted with it stand in for the few tokens a provider uses to mark out messages and blocks), plus
`max_image_tokens` per image, plus `request_overhead_tokens`, plus, for a request that continues an earlier response by
reference, that response's reported input and output. They are priced at the dearest input rate (uncached, cache
write or cache read), and the request's maximum output at the output rate: a request without an output bound is never
made. The providers' token-counting endpoints are not used: neither installed SDK says they are free of charge.

THE LEDGER (`eprbench ledger init FILE --ceiling GBP`; then `--ledger FILE` on every paid run). JSON lines, each with a
sequence number, the previous line's hash and its own: init (format, id, ceiling), raise (a higher ceiling, with a
reason), reserve, reconcile and episode. Every reservation, reconciliation and ceiling change is appended under an
exclusive lock (fcntl) after the whole ledger has been read again and validated (strict types, finite non-negative
amounts, known kinds, consecutive sequence numbers, an intact hash chain); the line is flushed and fsynced before the
request is sent. A ledger that does not validate stops everything. Spent = the actual cost of every reconciled request
+ the reserved amount of every other reservation: one that failed, timed out or whose response could not be read
(kept), and one left by a process that stopped before reconciling.

BACKSTOPS that do not depend on the input estimate, also checked under the lock: the ledger's total of requests at most
ceiling / (the cheapest a request can be: its overhead, at least one token, at the cheapest input rate); its total of
reserved output tokens at most ceiling / the output rate; and no request whose serialised size, images included,
exceeds `max_request_bytes`.
"""

import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import time
import uuid
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path

CLASSES = ("input", "cache_write", "cache_read", "output")
LEDGER_FORMAT = "eprbench-ledger/1"
KINDS = ("init", "raise", "reserve", "reconcile", "episode", "bind", "override")
MAX_REQUEST_BYTES = 20_000_000  # serialised, images included: below the providers' own request size limits
GENESIS = "0" * 64


class SpendError(ValueError):
    """The prices, the ceiling or the ledger cannot be used: nothing is run."""


class LedgerError(SpendError):
    """The ledger is missing, unreadable or does not validate."""


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _count(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool) and x >= 0


# --- prices ----------------------------------------------------------------------------------------------------------


class Prices:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        try:
            raw = self.path.read_bytes()
            data = json.loads(raw, parse_constant=lambda c: (_ for _ in ()).throw(SpendError(f"{path}: {c} is not a price")))
        except (OSError, ValueError) as e:
            raise SpendError(f"{path}: cannot be read as JSON ({e})") from None
        self.sha256 = hashlib.sha256(raw).hexdigest()
        if not isinstance(data, dict) or not isinstance(data.get("models"), dict):
            raise SpendError(f"{path}: needs models, prices by model id")
        self.currency, rate = data.get("currency"), data.get("gbp_per_unit")
        if self.currency == "GBP":
            if rate != 1 or isinstance(rate, bool) or "fx_safety_margin" in data:
                raise SpendError(f"{path}: prices in GBP need gbp_per_unit exactly 1 and no exchange margin")
            self.conversion = 1.0
        elif isinstance(self.currency, str) and re.fullmatch(r"[A-Z]{3}", self.currency):
            margin = data.get("fx_safety_margin")
            if not (_finite(rate) and 0.1 <= rate <= 10):
                raise SpendError(f"{path}: gbp_per_unit must be a conservative rate into pounds between 0.1 and 10 (got {rate!r})")
            if not (_finite(margin) and 0 < margin <= 1):
                raise SpendError(f"{path}: prices in {self.currency} need fx_safety_margin, above 0 and at most 1")
            self.conversion = float(rate) * (1 + float(margin))
        else:
            raise SpendError(f"{path}: currency must be GBP or a three-letter currency code")
        self.models = data["models"]

    def model(self, model_id: str) -> "ModelPrice":
        entry = self.models.get(model_id)
        if not isinstance(entry, dict) or not isinstance(entry.get("per_million_tokens"), dict):
            raise SpendError(f"{self.path} has no prices for model {model_id!r}: add them before a paid run")
        per = entry["per_million_tokens"]
        bad = [c for c in CLASSES if not (_finite(per.get(c)) and per[c] > 0)]
        if bad or set(per) - set(CLASSES):
            raise SpendError(f"{self.path}: model {model_id!r} needs a price above zero per million tokens for each of {', '.join(CLASSES)}"
                             + (f" (wrong: {', '.join(bad)})" if bad else f" and nothing else (unknown: {', '.join(sorted(set(per) - set(CLASSES)))})"))  # fmt: skip
        images, overhead = entry.get("max_image_tokens"), entry.get("request_overhead_tokens")
        if not (_count(images) and images > 0):
            raise SpendError(f"{self.path}: model {model_id!r} needs max_image_tokens, the most tokens one image can cost (a whole number above 0)")
        if not _count(overhead):
            raise SpendError(f"{self.path}: model {model_id!r} needs request_overhead_tokens, the tokens the provider adds to every request (a whole number)")
        return ModelPrice(model_id, {c: per[c] * self.conversion / 1e6 for c in CLASSES}, images, overhead)


class ModelPrice:
    """One model's prices in pounds per token, and its per-request allowances."""

    def __init__(self, model_id: str, gbp: dict, max_image_tokens: int, request_overhead_tokens: int):
        self.model_id, self.gbp, self.max_image_tokens, self.overhead = model_id, gbp, max_image_tokens, request_overhead_tokens
        self.input_max = max(gbp["input"], gbp["cache_write"], gbp["cache_read"])  # whatever class the provider bills it in
        self.input_min = min(gbp["input"], gbp["cache_write"], gbp["cache_read"])

    # whole numbers of what the ceiling could buy at the cheapest rate; the tiny tolerance absorbs binary rounding of the
    # division (0.02 / 1e-5 is 1999.9999...), never a whole token or request
    def max_requests(self, ceiling: float) -> int:
        return math.floor(ceiling / (max(self.overhead, 1) * self.input_min) * (1 + 1e-12))

    def max_output_tokens(self, ceiling: float) -> int:
        return math.floor(ceiling / self.gbp["output"] * (1 + 1e-12))


def token_classes(usage: dict) -> dict:
    """Recorded usage by token class: Claude's input excludes its cache writes and reads; OpenAI's includes its cached
    (read) tokens, which are taken out of it. Only for usage check_usage accepted."""
    if "cached_input_tokens" in usage:
        cached = usage["cached_input_tokens"]
        return {"input": usage["input_tokens"] - cached, "cache_write": 0, "cache_read": cached, "output": usage["output_tokens"]}
    return {"input": usage["input_tokens"], "cache_write": usage["cache_creation_input_tokens"], "cache_read": usage["cache_read_input_tokens"],
            "output": usage["output_tokens"]}  # fmt: skip


USAGE_FIELDS = {"anthropic": ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"),
                "openai": ("input_tokens", "output_tokens", "cached_input_tokens")}  # fmt: skip


def check_usage(usage, provider: str, bound: dict, previous_input: int | None) -> str | None:
    """Why a response's reported usage cannot be trusted to replace its reservation, or None if it can: every token
    class this provider reports must be present, a whole number and not negative; a request, which is never empty,
    must report some input; and a request continuing an earlier response by reference must report at least the input
    of the last response whose usage was confirmed, which it carries again. None of these can reject usage a provider
    really reported: there is no floor on tokens per byte, which no tokeniser guarantees."""
    if not isinstance(usage, dict):
        return "no usage was reported"
    for field in USAGE_FIELDS.get(provider, ("input_tokens", "output_tokens")):
        v = usage.get(field)
        if not (isinstance(v, int) and not isinstance(v, bool) and v >= 0):
            return f"usage field {field} is missing or not a whole number of tokens ({v!r})"
    if provider == "openai" and usage["cached_input_tokens"] > usage["input_tokens"]:
        return "more cached input tokens than input tokens"
    total_input = usage["input_tokens"] + (usage.get("cache_creation_input_tokens", 0) + usage.get("cache_read_input_tokens", 0) if provider == "anthropic" else 0)
    if total_input <= 0:
        return f"no input tokens reported for a request of {bound['text_bytes']} bytes"
    if bound.get("continues") and previous_input is not None and total_input < previous_input:
        return f"input of {total_input} tokens is below the {previous_input} confirmed for the context it continues"
    return None


# --- the bound on one request --------------------------------------------------------------------------------------


MAX_IMAGE_WIDTH, MAX_IMAGE_HEIGHT = 1280, 800  # the browser harnesses' viewport: max_image_tokens is priced for this
_PNG = b"\x89PNG\r\n\x1a\n"
_DATA_URL = "data:image/png;base64,"


def _plain(obj):
    """Request parameters as plain JSON values (SDK objects dumped as the SDK would send them)."""
    if isinstance(obj, dict):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    if obj is None or isinstance(obj, (str, bool, int, float)):
        return obj
    if hasattr(obj, "model_dump"):
        return _plain(obj.model_dump(mode="json", exclude_none=True))
    if hasattr(obj, "__dict__"):
        return _plain({k: v for k, v in vars(obj).items() if not k.startswith("_")})
    return repr(obj)


def real_image(data) -> bool:
    """Whether base64 `data` decodes to a PNG no larger than the harness viewport (what max_image_tokens is priced for)."""
    import base64
    import binascii
    import struct

    if not isinstance(data, str) or not data:
        return False
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return False
    if raw[:8] != _PNG:
        return False
    import zlib

    at, kinds, width, height = 8, [], 0, 0
    while at + 12 <= len(raw):  # every chunk whole, its CRC right; IHDR first, IDAT, IEND last and at the very end
        (length,) = struct.unpack(">I", raw[at : at + 4])
        kind, body = raw[at + 4 : at + 8], raw[at + 8 : at + 8 + length]
        if len(body) != length or at + 12 + length > len(raw) or struct.unpack(">I", raw[at + 8 + length : at + 12 + length])[0] != zlib.crc32(kind + body):
            return False
        if kind == b"IHDR":
            if kinds or length != 13:
                return False
            width, height = struct.unpack(">II", body[:8])
        kinds.append(kind)
        at += 12 + length
        if kind == b"IEND":
            break
    complete = kinds[:1] == [b"IHDR"] and b"IDAT" in kinds and kinds[-1:] == [b"IEND"] and at == len(raw)
    return complete and 0 < width <= MAX_IMAGE_WIDTH and 0 < height <= MAX_IMAGE_HEIGHT


def _image_slots(plain: dict):
    """Each place an image may be in the requests the harnesses send, as (container, key, base64 data): an Anthropic
    image block (in a user message, or in a tool result's content), an OpenAI input_image or computer screenshot.
    Nothing else is ever an image."""
    def anthropic_block(b):
        src = b.get("source") if isinstance(b, dict) and b.get("type") == "image" else None
        if isinstance(src, dict) and src.get("type") == "base64" and src.get("media_type") == "image/png":
            yield src, "data", src.get("data")

    for m in plain.get("messages") or [] if isinstance(plain.get("messages"), list) else []:
        if isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("content"), list):
            for b in m["content"]:
                yield from anthropic_block(b)
                if isinstance(b, dict) and b.get("type") == "tool_result" and isinstance(b.get("content"), list):
                    for inner in b["content"]:
                        yield from anthropic_block(inner)
    for item in plain.get("input") if isinstance(plain.get("input"), list) else []:
        if not isinstance(item, dict):
            continue
        out = item.get("output")
        if item.get("type") == "computer_call_output" and isinstance(out, dict) and out.get("type") == "computer_screenshot":
            url = out.get("image_url")
            if isinstance(url, str) and url.startswith(_DATA_URL):
                yield out, "image_url", url[len(_DATA_URL):]
        if item.get("role") == "user" and isinstance(item.get("content"), list):
            for part in item["content"]:
                url = part.get("image_url") if isinstance(part, dict) and part.get("type") == "input_image" else None
                if isinstance(url, str) and url.startswith(_DATA_URL):
                    yield part, "image_url", url[len(_DATA_URL):]


def images_in(params: dict) -> list[str]:
    """The base64 data of every real image the request carries, in its image fields only."""
    return [data for _, _, data in _image_slots(_plain(params)) if real_image(data)]


def measure(params: dict) -> dict:
    """The request's text in UTF-8 bytes (everything but real images in image fields, which are counted, not measured),
    its images, and its whole serialised size."""
    plain = _plain({k: v for k, v in params.items() if k != "timeout"})
    images, image_bytes = 0, 0
    for holder, key, data in list(_image_slots(plain)):
        if real_image(data):
            images += 1
            image_bytes += len(holder[key])
            holder[key] = ""
    text_bytes = len(json.dumps(plain, ensure_ascii=False, separators=(",", ":")).encode())
    return {"text_bytes": text_bytes, "images": images, "serialised_bytes": text_bytes + image_bytes}


# --- the ledger ------------------------------------------------------------------------------------------------------


def _canonical(entry: dict) -> str:
    return json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _hash(entry: dict) -> str:
    return hashlib.sha256(_canonical({k: v for k, v in entry.items() if k != "hash"}).encode()).hexdigest()


def _wall() -> str:
    return datetime.fromtimestamp(time.time(), UTC).isoformat(timespec="milliseconds")


class State:
    """A ledger read and validated whole."""

    def __init__(self):
        self.seq, self.last_hash, self.ceiling, self.ledger_id = 0, GENESIS, None, None
        self.reservations: dict[str, dict] = {}
        self.hashes: list[str] = []  # each entry's hash, by sequence number - 1
        self.plans: set[str] = set()  # the plans (SHA-256) this ledger serves
        self.raised_at: list[int] = []  # the sequence numbers of ceiling raises
        self.overrides: list[str] = []  # what each recorded override was for

    def spent(self, where=lambda r: True) -> float:
        return sum(r["settled_gbp"] if r["settled_gbp"] is not None else r["gbp"] for r in self.reservations.values() if where(r))

    def requests(self) -> int:
        return len(self.reservations)

    def output_reserved(self) -> int:
        return sum(r["max_output_tokens"] for r in self.reservations.values())

    def outstanding(self) -> list[str]:
        return [rid for rid, r in self.reservations.items() if r["outcome"] is None]


def _need(cond, where: str, what: str) -> None:
    if not cond:
        raise LedgerError(f"ledger entry {where}: {what}")


def replay(lines: list[str]) -> State:
    """Validate a ledger's lines and return its state; any fault raises LedgerError."""
    s = State()
    for n, line in enumerate(lines, 1):
        try:
            e = json.loads(line, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(f"{c} is not a number")))
        except ValueError as err:
            raise LedgerError(f"ledger line {n} is not valid JSON ({err})") from None
        where = f"{n}"
        _need(isinstance(e, dict), where, "not an object")
        _need(e.get("seq") == n and _count(e.get("seq")), where, f"sequence number {e.get('seq')!r}, expected {n}")
        _need(e.get("prev") == s.last_hash, where, "does not follow the line before it (hash chain broken)")
        _need(isinstance(e.get("hash"), str) and e["hash"] == _hash(e), where, "its hash does not match its content (altered or corrupt)")
        kind = e.get("kind")
        _need(kind in KINDS, where, f"unknown kind {kind!r}")
        _need((n == 1) == (kind == "init"), where, "a ledger begins with one init entry and has no other")
        if kind == "init":
            _need(e.get("format") == LEDGER_FORMAT and isinstance(e.get("ledger_id"), str), where, "not an eprbench ledger")
            _need(_finite(e.get("ceiling_gbp")) and e["ceiling_gbp"] > 0, where, "the ceiling must be a positive amount")
            s.ceiling, s.ledger_id = float(e["ceiling_gbp"]), e["ledger_id"]
        elif kind == "raise":
            _need(_finite(e.get("ceiling_gbp")) and e["ceiling_gbp"] > s.ceiling, where, "a raise must name a higher ceiling")
            _need(isinstance(e.get("reason"), str) and e["reason"].strip(), where, "a raise must give its reason")
            s.ceiling = float(e["ceiling_gbp"])
            s.raised_at.append(n)
        elif kind == "bind":
            _need(isinstance(e.get("plan_sha256"), str) and len(e["plan_sha256"]) == 64, where, "a binding names a plan by its SHA-256")
            s.plans.add(e["plan_sha256"])
        elif kind == "override":
            _need(isinstance(e.get("reason"), str) and e["reason"].strip() and isinstance(e.get("what"), str), where, "an override says what and why")
            s.overrides.append(e["what"])
        elif kind == "reserve":
            rid = e.get("rid")
            _need(isinstance(rid, str) and rid and rid not in s.reservations, where, "a reservation needs a new id")
            _need(_finite(e.get("gbp")) and e["gbp"] >= 0, where, "a reservation's amount must be finite and not negative")
            _need(_count(e.get("max_output_tokens")) and _count(e.get("input_tokens_bound")), where, "token bounds must be whole numbers")
            for k in ("episode", "invocation"):
                _need(isinstance(e.get(k), str), where, f"{k} must be named")
            s.reservations[rid] = {"gbp": float(e["gbp"]), "settled_gbp": None, "outcome": None, "max_output_tokens": e["max_output_tokens"],
                                   "episode": e["episode"], "invocation": e["invocation"], "seq": n}  # fmt: skip
        elif kind == "reconcile":
            r = s.reservations.get(e.get("rid"))
            _need(r is not None and r["outcome"] is None, where, "reconciles no open reservation")
            _need(e.get("outcome") in ("confirmed", "kept"), where, "outcome must be confirmed or kept")
            _need(_finite(e.get("gbp")) and e["gbp"] >= 0, where, "an amount must be finite and not negative")
            _need(e["outcome"] == "confirmed" or e["gbp"] == r["gbp"], where, "a kept reservation keeps its whole amount")
            r["outcome"], r["settled_gbp"] = e["outcome"], float(e["gbp"])
        else:  # episode: a summary, which counts for nothing
            _need(_finite(e.get("gbp")) and e["gbp"] >= 0 and isinstance(e.get("episode"), str), where, "an episode summary needs its amount")
        s.seq, s.last_hash = n, e["hash"]
        s.hashes.append(e["hash"])
    _need(s.ceiling is not None, "0", "the ledger is empty")
    return s


def state_dir() -> Path:
    """Where ledger anchors are kept: EPRBENCH_STATE_DIR, else $XDG_STATE_HOME/eprbench, else ~/.local/state/eprbench. It
    is outside any results directory, so restoring or replacing a ledger file does not restore its anchor."""
    if os.environ.get("EPRBENCH_STATE_DIR"):
        return Path(os.environ["EPRBENCH_STATE_DIR"])
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "eprbench"


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(json.dumps(data, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _writable(directory: Path) -> None:
    """Fail now, before the ledger changes, if an anchor could not be written in `directory`."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    probe = directory / f".probe-{os.getpid()}-{secrets.token_hex(4)}"
    try:
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        os.unlink(probe)
    except OSError as e:
        raise LedgerError(f"the anchor directory {directory} cannot be written ({e}): nothing has been appended to the ledger") from None


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except ValueError as e:
        raise LedgerError(f"{path} cannot be read ({e}): a ledger anchor is damaged") from None


class Ledger:
    """One ledger file. Every change is made by `append` under an exclusive lock, after replaying the whole file.

    Anchors (state_dir()) remember, for each ledger id, the furthest it has reached (sequence number and hash), and for
    each path, the ledger id that lives there; both are written, fsynced, under the ledger's lock after every append.
    A ledger behind its anchor (an earlier copy restored), not continuing it, or with another id at a known path (a
    file replaced) is refused. The lock is taken on the opened file, which must still be the file at the path."""

    def __init__(self, path: str | Path, adopt_reason: str | None = None):
        self.path = Path(path)
        if not self.path.is_file():
            raise LedgerError(f"no ledger at {self.path}: create it once with eprbench ledger init {self.path} --ceiling GBP")
        self._adopt = (adopt_reason or "").strip() or None
        self.read()  # validates now, so a bad ledger stops a run before anything is set up

    @classmethod
    def adopt(cls, path: str | Path, reason: str) -> "Ledger":
        """Anchor a ledger that has entries but no anchor here (moved from another machine, or its anchors lost), after
        validating it whole: recorded in the ledger with the reason. The operator vouches that it is the latest copy."""
        if not reason.strip():
            raise SpendError("adopting a ledger needs the reason")
        return cls(path, adopt_reason=reason)

    @staticmethod
    def _path_record(path: Path) -> Path:
        return state_dir() / "paths" / (hashlib.sha256(str(path.resolve()).encode()).hexdigest() + ".json")

    @staticmethod
    def _anchor(ledger_id: str) -> Path:
        return state_dir() / "ledgers" / f"{ledger_id}.json"

    @classmethod
    def init(cls, path: str | Path, ceiling_gbp: float, replace_reason: str | None = None) -> "Ledger":
        path = Path(path)
        if not (_finite(ceiling_gbp) and ceiling_gbp > 0):
            raise SpendError("the ceiling must be a positive amount in pounds")
        path.parent.mkdir(parents=True, exist_ok=True)
        before = _read_json(cls._path_record(path))
        if before is not None and not (replace_reason or "").strip():
            raise LedgerError(f"{path} already had a ledger ({before['ledger_id']}): a new one there needs --replace-reason, and it is recorded")
        entry = {"seq": 1, "prev": GENESIS, "kind": "init", "format": LEDGER_FORMAT, "ledger_id": secrets.token_hex(8), "ceiling_gbp": float(ceiling_gbp),
                 "wall": _wall()} | ({"replaces": {"ledger_id": before["ledger_id"], "reason": replace_reason}} if before is not None else {})  # fmt: skip
        entry["hash"] = _hash(entry)
        _writable(cls._anchor(entry["ledger_id"]).parent)  # a ledger is never made without its anchors
        _writable(cls._path_record(path).parent)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise LedgerError(f"{path} already exists: a ledger is created once") from None
        with os.fdopen(fd, "w") as f:
            f.write(_canonical(entry) + "\n")
            f.flush()
            os.fsync(f.fileno())
        _fsync_dir(path.parent)
        _write_json(cls._path_record(path), {"path": str(path.resolve()), "ledger_id": entry["ledger_id"]})
        _write_json(cls._anchor(entry["ledger_id"]), {"ledger_id": entry["ledger_id"], "seq": 1, "hash": entry["hash"], "path": str(path.resolve())})
        return cls(path)

    def read(self) -> State:
        with self._locked() as (state, _):
            return state

    @contextmanager
    def _locked(self):
        with open(self.path, "r+b") as f, ExitStack() as stack:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                held, now = os.fstat(f.fileno()), os.stat(self.path)
                if (held.st_dev, held.st_ino) != (now.st_dev, now.st_ino):
                    raise LedgerError(f"{self.path} was replaced while its lock was awaited: the lock no longer guards it")
                raw = f.read()
                if raw and not raw.endswith(b"\n"):
                    raise LedgerError(f"{self.path}: its last line is incomplete (a write was cut off)")
                state = replay(raw.decode().splitlines())
                # One lock per ledger id on this machine, beside its anchor, held from the anchor check to the last append:
                # copies of one ledger at different paths have different file locks, but only one of them can be here.
                id_lock = self._anchor(state.ledger_id).with_suffix(".lock")
                id_lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                id_fd = os.open(id_lock, os.O_RDWR | os.O_CREAT, 0o600)
                stack.callback(os.close, id_fd)
                fcntl.flock(id_fd, fcntl.LOCK_EX)
                stack.callback(fcntl.flock, id_fd, fcntl.LOCK_UN)
                adopting = self._check_anchors(state)  # the anchor read again, inside the id lock

                def append(entry: dict) -> dict:
                    _writable(self._anchor(state.ledger_id).parent)  # an anchor that could not follow would leave the ledger unguarded
                    entry = {**entry, "seq": state.seq + 1, "prev": state.last_hash, "wall": _wall()}
                    entry["hash"] = _hash(entry)
                    f.seek(0, os.SEEK_END)
                    f.write((_canonical(entry) + "\n").encode())
                    f.flush()
                    os.fsync(f.fileno())
                    state.seq, state.last_hash = entry["seq"], entry["hash"]
                    state.hashes.append(entry["hash"])
                    _write_json(self._anchor(state.ledger_id), {"ledger_id": state.ledger_id, "seq": state.seq, "hash": state.last_hash,
                                                                "path": str(self.path.resolve())})  # fmt: skip
                    return entry

                if adopting:
                    _write_json(self._path_record(self.path), {"path": str(self.path.resolve()), "ledger_id": state.ledger_id})
                    append({"kind": "override", "what": "adopt", "reason": self._adopt})
                    self._adopt = None
                yield state, append
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def _check_anchors(self, state: State) -> bool:
        """Refuse a ledger its anchors do not vouch for. Returns whether it is being adopted (no anchors, by request)."""
        record = _read_json(self._path_record(self.path))
        if record is not None and record.get("ledger_id") != state.ledger_id:
            raise LedgerError(f"{self.path} is not the ledger that was at this path ({record.get('ledger_id')}), but {state.ledger_id}: "
                              "a ledger file was replaced")  # fmt: skip
        anchor = _read_json(self._anchor(state.ledger_id))
        if anchor is not None:
            if state.seq < anchor["seq"]:
                raise LedgerError(f"{self.path} is behind its anchor: it ends at entry {state.seq}, but entry {anchor['seq']} was written "
                                  "(an earlier copy was restored)")  # fmt: skip
            if state.hashes[anchor["seq"] - 1] != anchor["hash"]:
                raise LedgerError(f"{self.path} does not continue its anchor at entry {anchor['seq']}: its history was rewritten")
        if record is None or anchor is None:  # anchors are made at init: a ledger with entries but none here is not vouched for
            if self._adopt is None:
                raise LedgerError(f"{self.path} has no anchor in {state_dir()} (it ends at entry {state.seq}): it may be an earlier copy, or come from "
                                  "another machine. If it is the latest, eprbench ledger adopt FILE --reason TEXT (recorded)")  # fmt: skip
            return True
        return False

    def transact(self, fn):
        """Run fn(state, append) under the lock and return what it returns."""
        with self._locked() as (state, append):
            return fn(state, append)

    def raise_ceiling(self, ceiling_gbp: float, reason: str) -> dict:
        def go(state, append):
            if not (_finite(ceiling_gbp) and ceiling_gbp > state.ceiling):
                raise SpendError(f"the new ceiling must be above the current £{state.ceiling:.2f}")
            if not reason.strip():
                raise SpendError("give the reason for raising the ceiling")
            return append({"kind": "raise", "ceiling_gbp": float(ceiling_gbp), "reason": reason})

        return self.transact(go)

    def bind_plan(self, plan_sha256: str) -> None:
        """Record that this ledger serves a plan (once)."""
        self.transact(lambda state, append: None if plan_sha256 in state.plans else append({"kind": "bind", "plan_sha256": plan_sha256}))

    def record_override(self, what: str, reason: str) -> dict:
        if not reason.strip():
            raise SpendError("an override needs its reason")
        return self.transact(lambda state, append: append({"kind": "override", "what": what, "reason": reason}))


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# --- an invocation's budget and an episode's meter -------------------------------------------------------------------


class Budget:
    """The spend settings of one `eprbench run` invocation, over its ledger: the ledger's ceiling always; optionally a
    lower ceiling for this invocation (`invocation_gbp`) and for each episode (`per_run_gbp`); a reserve of the ceiling
    kept for final chances; and the largest request that may be sent."""

    def __init__(self, prices: Prices, ledger: Ledger, *, invocation_gbp: float | None = None, per_run_gbp: float | None = None,
                 reserve_gbp: float = 0.0, max_request_bytes: int = MAX_REQUEST_BYTES):  # fmt: skip
        for name, v in (("--max-spend", invocation_gbp), ("--max-run-spend", per_run_gbp)):
            if v is not None and not (_finite(v) and v > 0):
                raise SpendError(f"{name} must be a positive amount in pounds")
        state = ledger.read()
        if not (_finite(reserve_gbp) and 0 <= reserve_gbp < state.ceiling):
            raise SpendError("the reserve must be at least nothing and less than the ledger's ceiling")
        if not (_count(max_request_bytes) and max_request_bytes > 0):
            raise SpendError("the largest request must be a positive number of bytes")
        self.prices, self.ledger, self.invocation_gbp, self.per_run = prices, ledger, invocation_gbp, per_run_gbp
        self.reserve, self.max_request_bytes = float(reserve_gbp), max_request_bytes
        self.invocation = f"{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"

    def describe(self) -> dict:
        state = self.ledger.read()
        return {"ledger": str(self.ledger.path), "ledger_id": state.ledger_id, "ceiling_gbp": state.ceiling, "spent_before_gbp": round(state.spent(), 6),
                "invocation_gbp": self.invocation_gbp, "per_run_gbp": self.per_run, "reserve_gbp": self.reserve, "max_request_bytes": self.max_request_bytes,
                "prices": str(self.prices.path), "prices_sha256": self.prices.sha256, "currency": self.prices.currency, "conversion_to_gbp": self.prices.conversion,
                "invocation": self.invocation}  # fmt: skip

    def can_start(self, model: str, max_output_tokens: int) -> bool:
        """Whether an episode could make even one request: its maximum output and the overhead alone must fit."""
        price = self.prices.model(model)
        least = max_output_tokens * price.gbp["output"] + price.overhead * price.input_max
        state = self.ledger.read()
        return least <= min([state.ceiling - self.reserve - state.spent(), *self._invocation_left(state)])

    def _invocation_left(self, state: State) -> list[float]:
        if self.invocation_gbp is None:
            return []
        return [self.invocation_gbp - state.spent(lambda r: r["invocation"] == self.invocation)]

    def meter(self, who: dict, task_id: str, repeat: int) -> "Meter":
        return Meter(self, who, task_id, repeat)


class Meter:
    """One episode's reservations and reconciliations."""

    def __init__(self, budget: Budget, who: dict, task_id: str, repeat: int):
        self.budget, self.who, self.task_id, self.repeat = budget, who, task_id, repeat
        self.price = budget.prices.model(who["model"])  # refused here if the prices file does not list it
        self.episode = f"{budget.invocation}-{uuid.uuid4().hex[:8]}"
        self.n = 0
        self.context = 0  # tokens of the last request and its response: what a request continuing it by reference carries
        self.previous_input: int | None = None  # the last request's reported input, which a continuing request carries again
        self.refused: dict | None = None
        self.seqs: list[int] = []
        self.open: dict[str, dict] = {}

    def bound(self, params: dict) -> dict:
        """The most this request could cost, with how it was reached."""
        size = measure(params)
        out = params.get("max_tokens") or params.get("max_output_tokens")
        continues = bool(params.get("previous_response_id"))
        tokens = size["text_bytes"] + size["images"] * self.price.max_image_tokens + self.price.overhead + (self.context if continues else 0)
        gbp = tokens * self.price.input_max + out * self.price.gbp["output"] if _count(out) and out > 0 else math.inf
        return {**size, "input_tokens_bound": tokens, "max_output_tokens": out if _count(out) else None, "gbp": gbp, "continues": continues}

    def reserve(self, params: dict, final_chance: bool) -> tuple[str | None, dict | None]:
        """Reserve this request's bound in the ledger: (reservation id, None), or (None, why it may not be sent)."""
        b = self.bound(params)

        def go(state: State, append):
            crossed = []
            if not math.isfinite(b["gbp"]):
                crossed.append("unbounded_output")
            else:
                if state.spent() + b["gbp"] > state.ceiling - (0 if final_chance else self.budget.reserve):
                    crossed.append("ledger_ceiling" if final_chance else "ledger_ceiling_less_reserve")
                if any(b["gbp"] > left for left in self.budget._invocation_left(state)):
                    crossed.append("invocation")
                if self.budget.per_run is not None and state.spent(lambda r: r["episode"] == self.episode) + b["gbp"] > self.budget.per_run:
                    crossed.append("per_run")
                if state.requests() + 1 > self.price.max_requests(state.ceiling):
                    crossed.append("backstop_requests")
                if state.output_reserved() + b["max_output_tokens"] > self.price.max_output_tokens(state.ceiling):
                    crossed.append("backstop_output_tokens")
            if b["serialised_bytes"] > self.budget.max_request_bytes:
                crossed.append("backstop_request_bytes")
            if crossed:
                return None, {"bound": {**b, "gbp": round(b["gbp"], 6) if math.isfinite(b["gbp"]) else None}, "crossed": crossed,
                              "spent_gbp": round(state.spent(), 6), "ceiling_gbp": state.ceiling, "final_chance": final_chance}  # fmt: skip
            self.n += 1
            rid = f"{self.episode}-{self.n}"
            e = append({"kind": "reserve", "rid": rid, "gbp": b["gbp"], "input_tokens_bound": b["input_tokens_bound"], "max_output_tokens": b["max_output_tokens"],
                        "text_bytes": b["text_bytes"], "images": b["images"], "episode": self.episode, "invocation": self.budget.invocation, "pid": os.getpid(),
                        "model": self.who["model"], "provider": self.who["provider"], "interface": self.who["interface"], "task_id": self.task_id,
                        "repeat": self.repeat, "final_chance": final_chance, "prices_sha256": self.budget.prices.sha256})  # fmt: skip
            self.seqs.append(e["seq"])
            self.open[rid] = b
            return rid, None

        rid, why = self.budget.ledger.transact(go)
        if why:
            self.refused = why
        return rid, why

    def settle(self, rid: str, usage: dict | None, failure: str | None = None) -> dict:
        """Reconcile a reservation. With complete, plausible usage (check_usage), its actual cost; otherwise (the attempt
        failed, timed out, its response could not be read, or its usage is missing, partial or implausible) the whole
        reservation is kept as spent, with the reason."""
        b = self.open.pop(rid)
        why = failure or check_usage(usage, self.who["provider"], b, self.previous_input)
        if why:
            cost = {"outcome": "kept", "gbp": b["gbp"], "reason": why}
            self.context = b["input_tokens_bound"] + b["max_output_tokens"]  # for the next bound only: the conservative side
            # previous_input stays the last confirmed figure: an unknown is never a floor for what follows
        else:
            tokens = token_classes(usage)
            gbp = sum(tokens[c] * self.price.gbp[c] for c in CLASSES)
            cost = {"outcome": "confirmed", "gbp": gbp, "tokens": tokens, "overrun": gbp > b["gbp"]}
            self.context = sum(tokens.values())
            self.previous_input = tokens["input"] + tokens["cache_write"] + tokens["cache_read"]
        e = self.budget.ledger.transact(lambda state, append: append({"kind": "reconcile", "rid": rid, **cost}))
        self.seqs.append(e["seq"])
        return {**cost, "gbp": round(cost["gbp"], 6), "reserved_gbp": round(b["gbp"], 6)}

    def spent(self) -> float:
        return self.budget.ledger.read().spent(lambda r: r["episode"] == self.episode)

    def summary(self) -> dict:
        state = self.budget.ledger.read()
        mine = [r for r in state.reservations.values() if r["episode"] == self.episode]
        return {"gbp": round(state.spent(lambda r: r["episode"] == self.episode), 6), "reservations": len(mine),
                "kept": sum(r["outcome"] == "kept" for r in mine), "unreconciled": sum(r["outcome"] is None for r in mine), "refused": self.refused,
                "ledger_id": state.ledger_id, "ledger_seq": [min(self.seqs), max(self.seqs)] if self.seqs else None, "ceiling": self.budget.describe()}  # fmt: skip

    def close(self, evidence_dir: str | None, terminal_reason: str | None) -> dict:
        """Write the episode's summary line; returns the ledger range its entries span."""
        for rid in list(self.open):  # cannot happen in the harnesses; if it does, the reservation is kept as spent
            self.settle(rid, None, "the episode ended with this request unreconciled")
        gbp = self.spent()
        e = self.budget.ledger.transact(lambda state, append: append({"kind": "episode", "episode": self.episode, "gbp": gbp, "task_id": self.task_id,
                                                                       "repeat": self.repeat, "model": self.who["model"], "interface": self.who["interface"],
                                                                       "evidence_dir": evidence_dir, "terminal_reason": terminal_reason}))  # fmt: skip
        self.seqs.append(e["seq"])
        return {"ledger_id": self.budget.ledger.read().ledger_id, "first_seq": min(self.seqs), "last_seq": e["seq"], "last_hash": e["hash"]}
