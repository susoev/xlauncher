"""Подбор отпечатка: проба движком заменена заглушкой с контролируемыми совпадениями."""

import pytest

from xlauncher.core import fingerprint, profiles, uniqueness


@pytest.fixture()
def fake_probe(monkeypatch, tmp_path):
    exe = tmp_path / "chrome.exe"
    exe.write_text("")
    monkeypatch.setenv("XL_ENGINE_EXE", str(exe))
    plan = {"queue": [], "calls": 0}

    def probe(exe, seed, hc=8, timeout=40):
        plan["calls"] += 1
        if seed is None:
            return {"audio": "BASE", "canvas": "basecanvas", "webgl": "gpu"}
        if plan["queue"]:
            audio, canvas = plan["queue"].pop(0)
        else:
            audio, canvas = f"a{seed}", f"c{seed}"
        return {"audio": audio, "canvas": canvas, "webgl": "gpu"}

    monkeypatch.setattr(uniqueness, "probe", probe)
    return plan


def test_unique_on_first_try(xl_root, fake_probe):
    fp, rep = uniqueness.pick(fingerprint.generate("RU"))
    assert rep["checked"] and rep["unique"] and rep["tried"] == 1
    assert fp["probe"]["audio"].startswith("a") and fp["probe"]["host"]


def test_regenerates_on_collision_with_other_profile(xl_root, fake_probe):
    first, _ = uniqueness.pick(fingerprint.generate())
    profiles.create("One", fp=first)
    fake_probe["queue"] = [(first["probe"]["audio"], "x1"), ("BASE", "x2")]  # audio-дубль, затем совпадение с базой
    fp, rep = uniqueness.pick(fingerprint.generate())
    assert rep["unique"] and rep["tried"] == 3
    assert fp["seed"] != first["seed"]
    assert fp["probe"]["audio"] not in (first["probe"]["audio"], "BASE")


def test_best_effort_when_all_collide(xl_root, fake_probe):
    first, _ = uniqueness.pick(fingerprint.generate())
    profiles.create("One", fp=first)
    a = first["probe"]["audio"]
    fake_probe["queue"] = [(a, first["probe"]["canvas"])] + [(a, f"c{i}") for i in range(10)]
    fp, rep = uniqueness.pick(fingerprint.generate(), attempts=4)
    assert rep["checked"] and not rep["unique"] and rep["tried"] == 4
    assert fp["probe"]["canvas"] != first["probe"]["canvas"]  # выбран вариант с меньшим совпадением
    assert any("audio" in x for x in rep["issues"])


def test_skipped_without_engine(xl_root, monkeypatch):
    monkeypatch.delenv("XL_ENGINE_EXE", raising=False)
    fp, rep = uniqueness.pick(fingerprint.generate())
    assert not rep["checked"] and "не установлен" in rep["reason"] and "probe" not in fp


def test_audit_reports_without_changing(xl_root, fake_probe):
    a = profiles.create("A")
    b = profiles.create("B")
    seeds = (a.data["fingerprint"]["seed"], b.data["fingerprint"]["seed"])
    fake_probe["queue"] = [("same", "c1"), ("same", "c2")]
    res = uniqueness.audit()
    assert res["checked"] == 2
    assert res["collisions"] == [{"kind": "audio", "profiles": ["A", "B"]}]
    assert (profiles.get(a.id).data["fingerprint"]["seed"], profiles.get(b.id).data["fingerprint"]["seed"]) == seeds
    calls = fake_probe["calls"]
    uniqueness.audit()  # повторно не пробует — пробы уже сохранены
    assert fake_probe["calls"] == calls


def test_clone_gets_unique_fingerprint(xl_root, fake_probe):
    from xlauncher.core import service

    src = profiles.create("Src", fp=uniqueness.pick(fingerprint.generate())[0])
    res = service.clone_profile(src.id, "Copy")
    assert res["uniqueness"]["unique"]
    assert res["profile"]["fingerprint"]["probe"]["audio"] != src.data["fingerprint"]["probe"]["audio"]
