"""provision: decide existing vs new datasets without touching a real Nominal tenant."""
import pytest

from nominal_bridge import provision as prov
from nominal_bridge.config import ConfigError


class Obj:
    def __init__(self, rid, name, labels=()):
        self.rid, self.name, self.labels = rid, name, list(labels)
        self.scopes = {}

    def list_datasets(self):
        return list(self.scopes.items())

    def add_dataset(self, scope, ds, series_tags=None):
        self.scopes[scope] = ds


class FakeClient:
    def __init__(self, datasets=(), assets=()):
        self.datasets = {d.rid: d for d in datasets}
        self.assets = {a.rid: a for a in assets}
        self.created = []

    def get_dataset(self, rid):
        if rid not in self.datasets:
            raise ValueError(f"dataset {rid!r} not found")
        return self.datasets[rid]

    def get_asset(self, rid):
        if rid not in self.assets:
            raise ValueError("not found")
        return self.assets[rid]

    def search_datasets(self, exact_match=None, search_text=None, labels=None):
        out = list(self.datasets.values())
        if exact_match:
            out = [d for d in out if d.name == exact_match]
        if search_text:
            out = [d for d in out if search_text.lower() in d.name.lower()]
        if labels:
            out = [d for d in out if set(labels) & set(d.labels)]
        return out

    def search_assets(self, search_text=None):
        return [a for a in self.assets.values() if search_text in a.name]

    def create_dataset(self, name, **kw):
        ds = Obj(f"new-ds-{len(self.created)}", name)
        self.datasets[ds.rid] = ds
        self.created.append(("dataset", name))
        return ds

    def create_asset(self, name, **kw):
        a = Obj(f"new-asset-{len(self.created)}", name)
        self.assets[a.rid] = a
        self.created.append(("asset", name))
        return a

    def create_run(self, name, **kw):
        self.created.append(("run", name))
        return Obj("new-run", name)


MODEL = {
    "labels": ["lab"],
    "datasets": {
        "uci": {"env": "NOMINAL_DS_UCI", "name": "Lab - UCI"},
        "sensor": {"env": "NOMINAL_DS_SENSOR", "name": "Lab - Sensors"},
    },
    "asset": {"env": "NOMINAL_ASSET_RID", "name": "Lab",
              "scopes": {"uci_c2": {"stream": "uci"}, "sensors": {"stream": "sensor"}}},
    "run": {"env": "NOMINAL_RUN_RID", "name": "Lab session"},
}


@pytest.fixture
def run(monkeypatch):
    def _run(client, **kw):
        monkeypatch.setattr(prov, "connect", lambda profile: client)
        return prov.provision(MODEL, profile="p", **kw)
    return _run


def test_stale_env_ids_are_ignored_not_fatal(run, monkeypatch, capsys):
    """The reported failure: .env held IDs from another workspace."""
    monkeypatch.setenv("NOMINAL_DS_UCI", "ri.catalog.other-tenant.dataset.x")
    monkeypatch.setenv("NOMINAL_ASSET_RID", "ri.scout.other-tenant.asset.y")
    existing = Obj("ds-uci", "Lab - UCI")
    client = FakeClient([existing])
    env = run(client)
    out = capsys.readouterr().out
    assert "ignored NOMINAL_DS_UCI from .env" in out
    assert env["NOMINAL_DS_UCI"] == "ds-uci"                   # fell back to the name match
    assert ("dataset", "Lab - Sensors") in client.created      # nothing to reuse: created
    assert ("asset", "Lab") in client.created


def test_env_ids_from_another_tenant_are_skipped_entirely(run, monkeypatch):
    monkeypatch.setenv("NOMINAL_DS_UCI", "ds-uci")
    client = FakeClient([Obj("ds-uci", "Something else")])
    env = run(client, env_ok=False)
    assert env["NOMINAL_DS_UCI"] != "ds-uci"


def test_valid_env_and_map_are_reused(run, monkeypatch):
    monkeypatch.setenv("NOMINAL_DS_UCI", "ds-a")
    client = FakeClient([Obj("ds-a", "Old UCI name"), Obj("ds-b", "Sensors 2025")])
    env = run(client, mapping={"sensor": "ds-b"})
    assert env["NOMINAL_DS_UCI"] == "ds-a" and env["NOMINAL_DS_SENSOR"] == "ds-b"
    assert not [c for c in client.created if c[0] == "dataset"]


def test_bad_map_is_an_error(run):
    with pytest.raises(ConfigError, match="--map uci=nope"):
        run(FakeClient(), mapping={"uci": "nope"})


def test_existing_mode_never_creates(run):
    client = FakeClient([Obj("ds-uci", "Lab - UCI")])
    with pytest.raises(ConfigError, match="no existing dataset for: sensor"):
        run(client, mode="existing")
    assert client.created == []


def test_new_mode_ignores_matches(run, monkeypatch):
    monkeypatch.setenv("NOMINAL_DS_UCI", "ds-uci")
    client = FakeClient([Obj("ds-uci", "Lab - UCI")])
    env = run(client, mode="new")
    assert env["NOMINAL_DS_UCI"] != "ds-uci"
    assert [c[1] for c in client.created if c[0] == "dataset"] == ["Lab - UCI", "Lab - Sensors"]


def test_picker_offers_similar_datasets(run):
    similar = Obj("ds-old", "Lab Sensors (Nov)", labels=["lab"])
    client = FakeClient([similar])
    offered = {}

    def choose(decision, candidates):
        offered[decision.key] = [c.name for c in candidates]
        return candidates[0] if decision.key == "sensor" else None
    env = run(client, choose=choose)
    assert offered["sensor"] == ["Lab Sensors (Nov)"]
    assert env["NOMINAL_DS_SENSOR"] == "ds-old"


def test_declining_changes_nothing(run):
    client = FakeClient()
    with pytest.raises(ConfigError, match="cancelled"):
        run(client, confirm=lambda: False)
    assert client.created == []
