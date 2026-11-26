"""«Забрать сюда»: машина B забирает все профили с машины A той же связки без человека у A.

Связка — все Windows-машины пользователя в одной сети Tailscale; ничего вводить не нужно.
Релей находится сам (опрос соседних машин tailnet на порту релея). Из id сети выводятся group_id и ключ
AES-256-GCM сообщений; запрос «забрать» машина A выполняет, только если релей видел его с адреса
Windows-машины того же пользователя Tailscale (A сверяет по своему `tailscale status`).

Ход дела:
  B → A  pull {req, from, from_name, pub}         B создал пару X25519 и ждёт
  A      закрывает запущенные профили сразу, упаковывает и загружает пакет (как «Отправить»)
  A → B  progress {req, stage, done, total}       пока идёт упаковка/загрузка
  A → B  ready {req, sealed}                      ключ переезда, зашифрованный открытым ключом B
  B      скачивает, проверяет каждый файл; свои профили — в backups/<дата>-before-move/; confirm на релее
  A      видит на релее «confirmed» и очищает себя (move.finish(auto=True) → загрузчик, код 76)
  B → A  cancel {req}                             отмена до приёма: A отменяет пакет и размораживается
  A → B  error {req, error}                       A не смог — профили на A разморожены
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import socket
import threading
import time
from pathlib import Path

from .. import __version__, paths, settings
from . import move, profiles, runstate

log = logging.getLogger("xlauncher.pull")

PAIR_PREFIX = "XLP"
PAIR_BYTES = 15  # 120 бит → 24 знака
BEAT_S = 60
POLL_IDLE_S = 15
POLL_ACTIVE_S = 3
ONLINE_S = 180
REQUEST_TTL_S = 15 * 60
CAPS = ["pull1"]


# ---------------------------------------------------------------- код связки


def new_pair_code(relay: str) -> str:
    body = move._b32(secrets.token_bytes(PAIR_BYTES))
    return f"{PAIR_PREFIX}-" + "-".join(body[i:i + 4] for i in range(0, len(body), 4)) + "@" + move.normalize_relay(relay)


def parse_pair_code(text: str) -> tuple[bytes, str]:
    t = "".join((text or "").split())
    if "@" not in t:
        raise move.MoveError("в коде связки нет адреса сервера (часть после @)")
    body, relay = t.rsplit("@", 1)
    body = body.upper().replace("-", "")
    if not body.startswith(PAIR_PREFIX):
        raise move.MoveError("это не код связки XLauncher (должен начинаться с XLP-)")
    body = body[len(PAIR_PREFIX):].translate(str.maketrans("OIL", "011"))
    if len(body) != PAIR_BYTES * 8 // 5 or any(c not in move.CROCKFORD for c in body):
        raise move.MoveError("код связки повреждён")
    return move._unb32(body), move.normalize_relay(relay)


class Pair:
    """Связка. Обычно автоматическая — все машины одной сети Tailscale (auto=True): секрет выводится из id сети,
    а запрос «забрать» принимается только с Windows-машины того же пользователя Tailscale (адрес отправителя
    фиксирует релей). Явный код связки XLP-… — для особых случаев (разные сети, без проверки по Tailscale)."""

    def __init__(self, code: str | None = None, *, secret: bytes | None = None, relay: str | None = None,
                 auto: bool = False) -> None:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF

        if code is not None:
            secret, relay = parse_pair_code(code)
        if secret is None or relay is None:
            raise ValueError("нужен код связки или секрет и релей")
        self.code, self.relay, self.auto = code, move.normalize_relay(relay), auto

        def kdf(info: str, n: int) -> bytes:
            return HKDF(algorithm=hashes.SHA256(), length=n, salt=None, info=info.encode()).derive(secret)

        self.gid = kdf("xl-pair v1 group", 16).hex()
        self.key = kdf("xl-pair v1 msg", 32)

    def seal(self, aad: str, obj: dict) -> dict:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        n = os.urandom(12)
        c = AESGCM(self.key).encrypt(n, json.dumps(obj, ensure_ascii=False).encode(), f"{self.gid}/{aad}".encode())
        return {"v": 1, "n": base64.b64encode(n).decode(), "c": base64.b64encode(c).decode()}

    def open(self, aad: str, env: dict) -> dict | None:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        try:
            pt = AESGCM(self.key).decrypt(base64.b64decode(env["n"]), base64.b64decode(env["c"]),
                                          f"{self.gid}/{aad}".encode())
            obj = json.loads(pt)
            return obj if isinstance(obj, dict) else None
        except (InvalidTag, KeyError, TypeError, ValueError):
            return None


def _move_settings() -> dict:
    return dict(settings.load().get("move") or {})


def device_id() -> str:
    m = _move_settings()
    if not m.get("device_id"):
        m["device_id"] = secrets.token_hex(16)
        settings.save({"move": m})
    return m["device_id"]


_net_cache: dict = {}


def network(max_age: float = 60) -> dict | None:
    """tailscale.network() с кешем: цикл связки опрашивает релей каждые несколько секунд."""
    from . import tailscale

    if _net_cache.get("at", 0) > time.time() - max_age:
        return _net_cache.get("net")
    try:
        net = tailscale.network()
    except Exception:  # noqa: BLE001
        net = None
    _net_cache.update(at=time.time(), net=net)
    return net


def auto_secret(tailnet: str) -> bytes:
    import hashlib

    return hashlib.sha256(b"xl-pair auto v1|" + tailnet.encode()).digest()


def pair(discover: bool = True) -> Pair | None:
    """discover=False — не искать релей по сети (для частых опросов из интерфейса)."""
    code = _move_settings().get("pair")
    if code:
        try:
            return Pair(code)
        except move.MoveError:
            return None
    net = network()
    if not net or not net.get("tailnet"):
        return None
    try:
        relay = move.relay_auto() if discover else move.relay_setting()
    except move.MoveError:
        return None
    if not relay:
        return None
    return Pair(secret=auto_secret(net["tailnet"]), relay=relay, auto=True)


def trusted_sender(ip: str | None) -> bool:
    """Запрос «забрать» — только с Windows-машины того же пользователя Tailscale, что и эта."""
    net = network(max_age=10)
    if not ip or not net:
        return False
    return any(ip in p["ips"] and p["os"] == "windows" and p["user"] == net["user"] for p in net["peers"])


def set_pair(code: str) -> dict:
    p = Pair(code)
    move.check_relay(p.relay)
    m = _move_settings()
    m.update(pair=p.code, relay=p.relay)
    settings.save({"move": m})
    device_id()
    worker.wake()
    return {"ok": True, "relay": p.relay}


def clear_pair() -> dict:
    p = pair()
    if p:
        try:
            _client(p).http.delete(f"/groups/{p.gid}/devices/{device_id()}")
        except Exception:  # noqa: BLE001
            pass
    m = _move_settings()
    m.pop("pair", None)
    settings.save({"move": m})
    return {"ok": True}


# ---------------------------------------------------------------- X25519 «запечатанный» ключ для B


def keypair() -> tuple[str, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    k = X25519PrivateKey.generate()
    raw = serialization.Encoding.Raw
    priv = k.private_bytes(raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    return priv.hex(), k.public_key().public_bytes(raw, serialization.PublicFormat.Raw).hex()


def _seal_key(shared: bytes, epub: bytes, pub: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(algorithm=hashes.SHA256(), length=32, salt=epub + pub, info=b"xl-pull v1 seal").derive(shared)


def seal_to(pub_hex: str, data: bytes) -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    pub = bytes.fromhex(pub_hex)
    eph = X25519PrivateKey.generate()
    epub = eph.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    k = _seal_key(eph.exchange(X25519PublicKey.from_public_bytes(pub)), epub, pub)
    return base64.b64encode(epub + AESGCM(k).encrypt(b"\0" * 12, data, None)).decode()


def unseal(priv_hex: str, sealed: str) -> bytes:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    raw = base64.b64decode(sealed)
    epub, ct = raw[:32], raw[32:]
    k = X25519PrivateKey.from_private_bytes(bytes.fromhex(priv_hex))
    pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    try:
        return AESGCM(_seal_key(k.exchange(X25519PublicKey.from_public_bytes(epub)), epub, pub)).decrypt(
            b"\0" * 12, ct, None)
    except InvalidTag:
        raise move.MoveError("ключ переезда от другой машины не расшифровался") from None


# ---------------------------------------------------------------- релей: связка


def _client(p: Pair, timeout: float = 15) -> move.Relay:
    return move.Relay(p.relay, timeout=timeout)


def send(p: Pair, to_dev: str, obj: dict) -> None:
    cl = _client(p)
    try:
        r = cl._call("POST", f"/groups/{p.gid}/inbox/{to_dev}", json=p.seal(f"inbox/{to_dev}", obj))
        if r.status_code != 201:
            raise move.MoveError(f"сообщение не доставлено: {cl._err(r)}")
    finally:
        cl.close()


def my_info() -> dict:
    items, _ = profiles.load_all()
    st = move.load_state()
    busy = None
    if st:
        busy = "moved" if st.get("phase") == "sent" else "sending"
    elif move.job.running:
        busy = move.job.snapshot().get("kind")
    return {"name": socket.gethostname(), "profiles": len(items), "version": __version__, "busy": busy,
            "caps": CAPS, "time": time.time()}


def beat(p: Pair) -> None:
    dev = device_id()
    cl = _client(p)
    try:
        cl._call("PUT", f"/groups/{p.gid}/devices/{dev}", retry=False, json=p.seal(f"device/{dev}", my_info()))
    finally:
        cl.close()


def devices() -> dict:
    """Машины связки (кроме этой) — для списка «Забрать сюда»."""
    p = pair()
    if not p:
        reason = ("Tailscale на этом компьютере не в сети — включите его" if not network(max_age=0)
                  else "Сервер переезда не найден в вашей сети Tailscale")
        return {"paired": False, "devices": [], "error": reason}
    me = device_id()
    cl = _client(p, timeout=10)
    try:
        r = cl._call("GET", f"/groups/{p.gid}/devices", retry=False)
        data = r.json()
    finally:
        cl.close()
    now = data.get("now") or time.time()
    out = []
    for d in data.get("devices") or []:
        dev = d.get("dev")
        info = p.open(f"device/{dev}", d.get("data") or {})
        if not info or dev == me:
            continue
        if p.auto and d.get("ip") and not trusted_sender(d.get("ip")):
            continue  # не Windows-машина владельца — забирать с неё нельзя
        ago = max(0, now - float(d.get("seen") or 0))
        out.append({"id": dev, "name": info.get("name"), "profiles": info.get("profiles"),
                    "version": info.get("version"), "busy": info.get("busy"), "seen_ago": round(ago),
                    "online": ago < ONLINE_S, "can_pull": "pull1" in (info.get("caps") or [])})
    out.sort(key=lambda x: (not x["online"], x["name"] or ""))
    return {"paired": True, "relay": p.relay, "devices": out}


# ---------------------------------------------------------------- B: запрос и приём


def pull_path() -> Path:
    return move.move_dir() / "pull.json"


def load_pull() -> dict | None:
    try:
        return json.loads(pull_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save_pull(st: dict) -> None:
    move.move_dir().mkdir(parents=True, exist_ok=True)
    paths.write_atomic(pull_path(), json.dumps(st, ensure_ascii=False, indent=2))


def pull_public() -> dict | None:
    st = load_pull()
    if not st:
        return None
    return {k: v for k, v in st.items() if k != "priv"}


def start_pull(target: str) -> dict:
    p = pair()
    if not p:
        raise move.MoveError("сначала введите код связки")
    if load_pull() and (load_pull() or {}).get("phase") not in ("done", "error", "cancelled"):
        raise move.MoveError("уже идёт «Забрать сюда»")
    if move.load_state():
        raise move.MoveError("с этого компьютера идёт свой переезд — отмените его")
    if move.job.running:
        raise move.MoveError("переезд уже выполняется")
    items, _ = profiles.load_all()
    running = [x.data.get("name") or x.id for x in items if runstate.is_active(x.id)]
    if running:
        raise move.MoveError("остановите профили этого компьютера: " + ", ".join(running[:5]))
    devs = {d["id"]: d for d in devices()["devices"]}
    d = devs.get(target)
    if not d:
        raise move.MoveError("машина не найдена в связке")
    if not d["can_pull"]:
        raise move.MoveError(f"на «{d['name']}» старая версия лаунчера — дождитесь её обновления")
    priv, pub = keypair()
    req = secrets.token_hex(8)
    st = {"req": req, "target": target, "target_name": d["name"], "priv": priv, "started": time.time(),
          "phase": "requested", "updated": time.time(), "error": None, "progress": None, "result": None}
    _save_pull(st)
    send(p, target, {"type": "pull", "req": req, "from": device_id(), "from_name": socket.gethostname(),
                     "pub": pub, "time": time.time()})
    worker.wake()
    return pull_public()


def cancel_pull() -> dict:
    st = load_pull()
    if not st:
        return {"ok": True}
    p = pair()
    if st["phase"] in ("receiving",):
        move.job.cancel()
    if p and st["phase"] not in ("done", "cancelled"):
        try:
            send(p, st["target"], {"type": "cancel", "req": st["req"], "time": time.time()})
        except move.MoveError as e:
            log.warning("отмена не доставлена: %s", e)
    pull_path().unlink(missing_ok=True)
    return {"ok": True}


def _receive_pulled(key: str, progress=None, cancel=None) -> dict:
    return move.receive(key, backup_existing=True, progress=progress, cancel=cancel)


def _on_b_message(p: Pair, msg: dict) -> None:
    st = load_pull()
    if not st or msg.get("req") != st["req"] or st["phase"] in ("done", "cancelled"):
        return
    t = msg.get("type")
    if t == "progress":
        st.update(phase=msg.get("stage") or "packing", progress=msg, updated=time.time())
        _save_pull(st)
    elif t == "error":
        st.update(phase="error", error=f"{st['target_name']}: {msg.get('error')}", updated=time.time())
        _save_pull(st)
    elif t == "ready":
        try:
            key = unseal(st["priv"], msg["sealed"]).decode()
            move.parse_key(key)
            st.update(phase="receiving", updated=time.time(), key=key)
            st.pop("priv", None)
            _save_pull(st)
            move.job.start("import", _receive_pulled, key)
        except (move.MoveError, KeyError, ValueError) as e:
            st.update(phase="error", error=str(e), updated=time.time())
            _save_pull(st)


def _check_b_receiving(p: Pair) -> None:
    """Приём закончился — отметить и сообщить A (A и сам увидит confirm на релее)."""
    st = load_pull()
    if not st or st["phase"] != "receiving" or move.job.running:
        return
    j = move.job.snapshot()
    if j.get("kind") != "import":
        return
    if j.get("stage") == "done":
        st.update(phase="done", result=j.get("result"), updated=time.time())
        st.pop("key", None)
        try:
            send(p, st["target"], {"type": "done", "req": st["req"], "time": time.time()})
        except move.MoveError:
            pass
    elif j.get("stage") in ("error", "cancelled"):
        st.update(phase="error", error=j.get("error") or "приём прерван", updated=time.time())
    _save_pull(st)


# ---------------------------------------------------------------- A: ответ на запрос


def _stop_all(timeout: float = 60) -> list[str]:
    from . import service

    items, _ = profiles.load_all()
    active = [x for x in items if runstate.is_active(x.id)]
    for x in active:
        service.stop(x.id)
    deadline = time.time() + timeout
    while time.time() < deadline and any(runstate.is_active(x.id) for x in active):
        time.sleep(0.5)
    return [x.data.get("name") or x.id for x in active if runstate.is_active(x.id)]


def _export_for(p: Pair, req: dict, progress=None, cancel=None) -> dict:
    to, rid = req["from"], req["req"]
    last = [0.0]

    def prog(stage, done, total, **kw):
        if progress:
            progress(stage, done, total, **kw)
        if time.time() - last[0] > 3 or done == total:
            last[0] = time.time()
            try:
                send(p, to, {"type": "progress", "req": rid, "stage": stage, "done": done, "total": total,
                             "bytes": kw.get("bytes"), "total_bytes": kw.get("total_bytes"),
                             "seconds": kw.get("seconds"), "time": time.time()})
            except move.MoveError:
                pass

    try:
        left = _stop_all()
        if left:
            raise move.MoveError("не закрылись профили: " + ", ".join(left[:5]))
        st = move.export(p.relay, progress=prog, cancel=cancel,
                         pull={"req": rid, "to": to, "to_name": req.get("from_name")})
        send(p, to, {"type": "ready", "req": rid, "sealed": seal_to(req["pub"], st["key"].encode()),
                     "time": time.time()})
        return st
    except BaseException as e:
        err = str(e) if isinstance(e, move.MoveError) else f"{type(e).__name__}: {e}"
        if move.load_state():
            try:
                move.cancel_export()  # разморозить: B не получил ключ
            except Exception:  # noqa: BLE001
                log.exception("отмена после ошибки")
        try:
            send(p, to, {"type": "error", "req": rid, "error": "отменено" if isinstance(e, move.Cancelled) else err,
                         "time": time.time()})
        except move.MoveError:
            pass
        raise


_handled: set[str] = set()


def _on_a_message(p: Pair, msg: dict) -> None:
    t = msg.get("type")
    if t == "pull":
        if time.time() - float(msg.get("time") or 0) > REQUEST_TTL_S:
            return  # старый запрос (машина была выключена) — не выполнять
        if msg.get("req") in _handled:
            return
        _handled.add(str(msg.get("req")))
        if p.auto and not trusted_sender(msg.get("_from_ip")):
            log.warning("«Забрать сюда»: запрос с %s отклонён — это не Windows-машина вашего Tailscale",
                        msg.get("_from_ip"))
            return
        st = move.load_state()
        if st and (st.get("pull") or {}).get("req") == msg.get("req"):
            return  # повтор
        if st or move.job.running:
            send(p, msg["from"], {"type": "error", "req": msg.get("req"),
                                  "error": "на этой машине уже идёт другой переезд", "time": time.time()})
            return
        log.warning("«Забрать сюда»: профили забирает %s — закрываю профили и отправляю", msg.get("from_name"))
        move.job.start("export", _export_for, p, msg)
    elif t == "cancel":
        st = move.load_state()
        if st and (st.get("pull") or {}).get("req") == msg.get("req"):
            log.info("«Забрать сюда» отменён на %s", (st.get("pull") or {}).get("to_name"))
            move.job.cancel()
            try:
                move.cancel_export()
            except move.MoveError as e:
                log.warning("отмена: %s", e)
        elif move.job.running and move.job.snapshot().get("kind") == "export":
            move.job.cancel()


def _check_a_moved() -> None:
    """A: другая машина подтвердила приём — очистить себя (решение владельца: только после проверки на B)."""
    st = move.load_state()
    if not st or not st.get("pull") or st.get("phase") != "sent":
        return
    status = move.remote_status()
    if status == "confirmed":
        log.warning("«Забрать сюда»: %s принял и проверил профили — очищаю этот компьютер",
                    st["pull"].get("to_name"))
        try:
            move.finish(auto=True)
        except move.MoveError as e:
            log.warning("очистка не началась: %s", e)
    elif status == "cancelled":
        move.state_path().unlink(missing_ok=True)


# ---------------------------------------------------------------- фоновый цикл


class Worker:
    def __init__(self) -> None:
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_beat = 0.0
        self._last_moved_check = 0.0

    def start(self) -> None:
        if not self._thread:
            self._thread = threading.Thread(target=self._run, name="pull", daemon=True)
            self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def tick(self) -> None:
        p = pair()
        if not p:
            return
        if time.time() - self._last_beat > BEAT_S:
            self._last_beat = time.time()
            beat(p)
        me = device_id()
        cl = _client(p, timeout=15)
        try:
            msgs = cl._call("GET", f"/groups/{p.gid}/inbox/{me}", retry=False).json().get("messages") or []
            for m in msgs:
                cl._call("DELETE", f"/groups/{p.gid}/inbox/{me}/{m['id']}", retry=False)
        finally:
            cl.close()
        for m in msgs:
            msg = p.open(f"inbox/{me}", m.get("data") or {})
            if not msg:
                continue
            msg["_from_ip"] = m.get("from_ip")
            try:
                if msg.get("type") in ("pull", "cancel", "done"):
                    _on_a_message(p, msg)
                else:
                    _on_b_message(p, msg)
            except Exception:  # noqa: BLE001
                log.exception("«Забрать сюда»: сообщение %s", msg.get("type"))
        _check_b_receiving(p)
        if time.time() - self._last_moved_check > 20:
            self._last_moved_check = time.time()
            _check_a_moved()

    def _run(self) -> None:
        fails = 0
        while True:
            try:
                self.tick()
                fails = 0
            except Exception as e:  # noqa: BLE001
                log.debug("связка: %s", e)
                fails += 1
                if fails % 20 == 5 and not _move_settings().get("pair"):
                    try:  # релей переехал или сменил адрес — поискать заново
                        move.relay_auto(refresh=True)
                    except Exception:  # noqa: BLE001
                        pass
            st = load_pull()
            active = (st and st.get("phase") not in ("done", "error", "cancelled")) or move.load_state() is not None
            self._wake.wait(POLL_ACTIVE_S if active else POLL_IDLE_S)
            self._wake.clear()


worker = Worker()
