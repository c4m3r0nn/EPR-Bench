import pytest

from eprbench import ops
from eprbench.tasks import build_world, load_task
from tests import agent_cli_guard

agent_cli_guard.activate()  # before any test module is collected: no test may start a real agent CLI

# completed imaging request forms for the demo patients
CTPA_ELLISON = {"clinical_question": "?PE", "egfr": "78", "egfr_date": "2026-03-10", "contrast_reaction": "no", "mobility": "bed"}
CTPA_ELLISTON = {"clinical_question": "?PE", "egfr": "22", "egfr_date": "2026-03-12", "contrast_reaction": "no", "mobility": "bed"}
CXR_FORM = {"clinical_question": "?consolidation", "mobility": "bed"}

def default_product(drug: str, route: str) -> str:
    """The first demo product of a drug that can be given by a route, so tests about other rules stay short."""
    from functools import cache

    from eprbench.loader import catalogue_formulary, read_bundle

    @cache
    def products():
        merged = {}
        for d in catalogue_formulary(read_bundle("demo")):  # the bundle's own entry for a drug adds no products
            merged.setdefault(d.name.lower(), []).extend(d.products)
        return merged

    canonical = ops._ROUTE_ALIASES.get(route.strip().upper(), route)
    candidates = products().get(drug.strip().lower(), [])
    for p in candidates:
        if canonical in p.routes:
            return p.name
    return candidates[0].name if candidates else ""  # no product suits the route: the route check should then refuse it


def with_product(args: dict) -> dict:
    """Prescription arguments with a product filled in when the test did not choose one."""
    if "product" in args or "drug" not in args or "route" not in args:
        return args
    return {**args, "product": default_product(args["drug"], args["route"])}


ELLISON, ELLISTON, WHITCOMBE = "H4417302", "H4471302", "H5120986"


@pytest.fixture
def task():
    return load_task("order-ctpa-001")


@pytest.fixture
def world(task):
    return build_world(task)


def in_hours(do, seconds: int):
    """Wait `seconds` as an agent must: repeated waits of at most ops.MAX_WAIT_SECONDS (an hour). Returns the last result."""
    out = None
    while seconds > 0:
        step = min(seconds, ops.MAX_WAIT_SECONDS)
        out, seconds = do(step), seconds - step
    return out


def calling(world, name, args, ctx=None):
    """ops.call, a long wait made as repeated waits of at most an hour, and a prescription given its default product."""
    if name == "wait" and args.get("seconds", 0) > ops.MAX_WAIT_SECONDS:
        return in_hours(lambda s: ops.call(world, "wait", {"seconds": s}, *([ctx] if ctx else [])), args["seconds"])
    return ops.call(world, name, with_product(args) if name == "prescribe_medication" else args, *([ctx] if ctx else []))


@pytest.fixture
def call(world):
    return lambda name, **args: calling(world, name, args)


@pytest.fixture
def pid(world):
    return lambda mrn: world.one("SELECT id FROM patient WHERE mrn = ?", (mrn,))["id"]


@pytest.fixture(autouse=True)
def _ledger_anchors(tmp_path_factory, monkeypatch):
    """Ledger anchors (eprbench.spend.state_dir) go to a directory of each test's own, never the user's state directory."""
    monkeypatch.setenv("EPRBENCH_STATE_DIR", str(tmp_path_factory.mktemp("state")))
