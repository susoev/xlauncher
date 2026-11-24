"""Форвардер против строгих тестовых SOCKS5 и HTTP-прокси с авторизацией."""

import asyncio
import base64
import socket
import urllib.request

import pytest

from xlauncher.core.proxy import Forwarder, ProxyError, open_tunnel, parse_proxy

U, P = b"user", b"secret"


async def _pipe(r, w):
    try:
        while d := await r.read(65536):
            w.write(d)
            await w.drain()
    except Exception:
        pass
    finally:
        w.close()


async def socks_server(r, w):
    _, n = await r.readexactly(2)
    methods = await r.readexactly(n)
    if 2 not in methods:
        w.write(b"\x05\xff")
        w.close()
        return
    w.write(b"\x05\x02")
    await r.readexactly(1)
    u = await r.readexactly((await r.readexactly(1))[0])
    p = await r.readexactly((await r.readexactly(1))[0])
    if (u, p) != (U, P):
        w.write(b"\x01\x01")
        w.close()
        return
    w.write(b"\x01\x00")
    _, _, _, atyp = await r.readexactly(4)
    assert atyp == 3, "DNS должен разрешаться на стороне прокси"
    host = (await r.readexactly((await r.readexactly(1))[0])).decode()
    port = int.from_bytes(await r.readexactly(2), "big")
    ur, uw = await asyncio.open_connection("127.0.0.1" if host == "target.test" else host, port)
    w.write(b"\x05\x00\x00\x01" + bytes(6))
    await asyncio.gather(_pipe(r, uw), _pipe(ur, w))


async def http_server(r, w):
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += await r.read(4096)
    head = buf.split(b"\r\n\r\n", 1)[0].split(b"\r\n")
    auth = [x for x in head if x.lower().startswith(b"proxy-authorization:")]
    if not auth or auth[0].split(b" ")[-1] != base64.b64encode(U + b":" + P):
        w.write(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
        w.close()
        return
    host, port = head[0].split(b" ")[1].decode().rsplit(":", 1)
    ur, uw = await asyncio.open_connection("127.0.0.1" if host == "target.test" else host, int(port))
    w.write(b"HTTP/1.1 200 OK\r\n\r\n")
    await asyncio.gather(_pipe(r, uw), _pipe(ur, w))


async def target_server(r, w):
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += await r.read(4096)
    w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 8\r\nConnection: close\r\n\r\nhello-xl")
    await w.drain()
    w.close()


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def servers():
    loop = asyncio.new_event_loop()
    ports = {}

    async def setup():
        for name, h in (("socks", socks_server), ("http", http_server), ("target", target_server)):
            srv = await asyncio.start_server(h, "127.0.0.1", 0)
            ports[name] = srv.sockets[0].getsockname()[1]

    loop.run_until_complete(setup())
    import threading

    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    yield ports
    loop.call_soon_threadsafe(loop.stop)


def _get_via(fwd_port: int, url: str) -> bytes:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{fwd_port}"}))
    return opener.open(url, timeout=10).read()


@pytest.mark.parametrize("scheme", ["socks5", "http"])
def test_forwarder_auth(servers, scheme):
    port = servers["socks" if scheme == "socks5" else "http"]
    target = f"http://target.test:{servers['target']}/"

    async def go(password):
        f = Forwarder(parse_proxy(f"{scheme}://user:{password}@127.0.0.1:{port}"))
        await f.start()
        try:
            return await asyncio.to_thread(_get_via, f.port, target), f.errors
        except Exception as e:  # noqa: BLE001
            return e, f.errors
        finally:
            await f.stop()

    body, _ = asyncio.run(go("secret"))
    assert body == b"hello-xl"
    res, errors = asyncio.run(go("wrong"))
    assert isinstance(res, Exception)
    assert any("логин или пароль" in e or "407" in e for e in errors), errors


def test_refused_port_is_humanized():
    port = _free_port()

    async def go():
        with pytest.raises(ProxyError) as ei:
            await open_tunnel(parse_proxy(f"127.0.0.1:{port}"), "example.com", 443)
        return str(ei.value)

    msg = asyncio.run(go())
    assert "отклонено" in msg or "не отвечает" in msg


def test_parse_formats():
    assert parse_proxy("") is None
    up = parse_proxy("socks5://u:p%40x@h:1080")
    assert (up.scheme, up.host, up.port, up.user, up.password) == ("socks5", "h", 1080, "u", "p@x")
    assert parse_proxy("h:1080:u:p").password == "p"
    assert parse_proxy("http://h:3128").scheme == "http"
    assert parse_proxy("u:p@h:1").scheme == "socks5"
    with pytest.raises(ValueError):
        parse_proxy("ftp://h:1")
    assert "***" in parse_proxy("h:1:u:p").masked()
