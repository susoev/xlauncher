import shutil

import pytest

from xlauncher.core import fingerprint, iphistory, profiles


def test_create_and_load(xl_root):
    p = profiles.create("Клиент: Instagram", group="Клиент", proxy="h:1:u:p", start_urls=[" https://x.com ", ""],
                        country="RU")
    assert p.dir.name == "Клиент-Instagram"
    assert len(p.id) == 26
    fp = p.data["fingerprint"]
    assert fp["locale"] == "ru-RU" and fp["accept_language"] == "ru-RU,ru,en-US,en"
    assert fp["platform"] == "windows" and fp["timezone"] == "auto"
    assert p.data["start_urls"] == ["https://x.com"]
    again = profiles.get(p.id)
    assert again.data == p.data


def test_unique_slugs(xl_root):
    a = profiles.create("Same")
    b = profiles.create("Same")
    assert a.dir.name == "Same" and b.dir.name == "Same-2"
    assert a.id != b.id


def test_update_rejects_unknown_fields(xl_root):
    p = profiles.create("P")
    profiles.update(p.id, {"name": "Q", "notes": "n"})
    assert profiles.get(p.id).data["name"] == "Q"
    with pytest.raises(ValueError):
        profiles.update(p.id, {"fingerprint": {}})


def test_clone_has_new_identity_and_no_cookies(xl_root):
    p = profiles.create("Src", country="US")
    cookies = p.dir / "data" / "Default" / "Network" / "Cookies"
    cookies.parent.mkdir(parents=True)
    cookies.write_bytes(b"secret")
    (p.dir / "data" / "Default" / "Bookmarks").write_text("{}")
    (p.dir / "os_crypt.key").write_bytes(b"k" * 32)
    iphistory.record(p.dir, "1.1.1.1", {"country": "US"}, "launch")

    c = profiles.clone(p.id, "Copy")
    assert c.id != p.id
    assert c.data["fingerprint"]["seed"] != p.data["fingerprint"]["seed"]
    assert c.data["fingerprint"]["locale"] == "en-US"
    assert not (c.dir / "data" / "Default" / "Network" / "Cookies").exists()
    assert not (c.dir / "os_crypt.key").exists()
    assert iphistory.read(c.dir) == []
    assert (c.dir / "data" / "Default" / "Bookmarks").exists()  # закладки остаются
    assert cookies.exists()  # исходник не тронут


def test_copied_folder_detected_as_duplicate(xl_root):
    p = profiles.create("Orig")
    shutil.copytree(p.dir, p.dir.with_name("Orig-copy"))
    items, dups = profiles.load_all()
    assert len(items) == 2 and set(dups) == {p.id}
    with pytest.raises(profiles.DuplicateError):
        profiles.get(p.id)
    profiles.new_identity(profiles.get_by_slug("Orig-copy"))
    _, dups = profiles.load_all()
    assert not dups
    assert profiles.get(p.id).dir.name == "Orig"


def test_delete_moves_to_backups(xl_root):
    p = profiles.create("Gone")
    dst = profiles.delete(p.id)
    assert dst.exists() and dst.parent.name == "backups"
    with pytest.raises(KeyError):
        profiles.get(p.id)


def test_iphistory_appends_only_on_change(xl_root):
    p = profiles.create("H")
    assert iphistory.record(p.dir, "1.1.1.1", {"country": "RU", "city": "Moscow"}, "launch")["changed"]
    assert not iphistory.record(p.dir, "1.1.1.1", {"country": "RU"}, "launch")["changed"]
    r = iphistory.record(p.dir, "2.2.2.2", {"country": "DE", "city": "Berlin"}, "monitor")
    assert r["changed"] and r["country_changed"]
    rows = iphistory.read(p.dir)
    assert [x["ip"] for x in rows] == ["1.1.1.1", "2.2.2.2"]
    assert rows[1]["flag"] == "COUNTRY_CHANGED" and rows[1]["source"] == "monitor"


def test_accept_language():
    assert fingerprint.accept_language("en-US") == "en-US,en"
    assert fingerprint.accept_language("th-TH") == "th-TH,th,en-US,en"
    assert fingerprint.locale_for(None) == "en-US"
