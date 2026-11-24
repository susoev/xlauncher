from fastapi.testclient import TestClient

from xlauncher.server import create_app


def test_profiles_crud(xl_root):
    c = TestClient(create_app(None))
    assert c.get("/api/health").json()["ok"]
    assert c.get("/api/profiles").json() == {"profiles": [], "duplicates": []}

    assert c.post("/api/profiles", json={"name": " "}).status_code == 400
    r = c.post("/api/profiles", json={"name": "A", "group": "G", "start_urls": ["https://x"]}).json()
    pid = r["profile"]["id"]
    assert r["check"] is None  # без прокси проверка не запускается

    lst = c.get("/api/profiles").json()["profiles"]
    assert len(lst) == 1 and lst[0]["state"] is None and "proxy" not in lst[0]

    c.patch(f"/api/profiles/{pid}", json={"proxy": "h:1:u:p"})
    assert c.get(f"/api/profiles/{pid}").json()["proxy_masked"] == "socks5://u:***@h:1"
    assert c.get(f"/api/profiles/{pid}?with_proxy=true").json()["proxy"] == "h:1:u:p"
    assert c.patch(f"/api/profiles/{pid}", json={"id": "x"}).status_code == 400

    clone = c.post(f"/api/profiles/{pid}/clone", json={"name": "B"}).json()
    assert clone["id"] != pid and clone["name"] == "B"

    assert c.get(f"/api/profiles/{pid}/ip-history").json() == {"history": []}
    assert c.delete(f"/api/profiles/{pid}").json()["ok"]
    assert c.get(f"/api/profiles/{pid}").status_code == 404
    assert "<title>xlauncher</title>" in c.get("/").text
