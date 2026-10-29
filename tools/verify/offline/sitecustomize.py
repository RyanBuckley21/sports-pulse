"""Network guard for the offline test suites: any socket connection to a
non-loopback address raises.

    PYTHONPATH=tools/verify/offline python3 -m tools.verify.test_nfl

WHY. Every suite under tools/verify is documented as offline and
deterministic, and CI (.github/workflows/tests.yml) runs them on every pull
request. A suite that quietly started calling a live API would still pass --
until the API had a bad morning, at which point CI goes red for a reason no
diff caused, and people learn to re-run it. Python imports `sitecustomize`
automatically from PYTHONPATH, so pointing PYTHONPATH here makes the offline
claim enforced rather than asserted: the first live request fails loudly,
naming the host.

LOOPBACK STAYS OPEN, and so any proxy on loopback does too. That is why the
2026-09-24 proof run in a proxied container also unset HTTPS_PROXY/HTTP_PROXY:
with the proxy at 127.0.0.1 the guard waved every request through and proved
nothing, which is exactly how that hole was found. GitHub's runners set no
proxy, so in CI there is no such path.
"""

import socket

_LOOPBACK = ("127.0.0.1", "::1", "localhost")
_connect = socket.socket.connect
_create_connection = socket.create_connection


def _refuse(addr):
    raise OSError("NETWORK BLOCKED -- offline test suite tried to reach %r "
                  "(see tools/verify/offline/sitecustomize.py)" % (addr,))


def _guarded_connect(self, addr, *args, **kwargs):
    host = addr[0] if isinstance(addr, tuple) else addr
    if host not in _LOOPBACK:
        _refuse(addr)
    return _connect(self, addr, *args, **kwargs)


def _guarded_create_connection(address, *args, **kwargs):
    if address[0] not in _LOOPBACK:
        _refuse(address)
    return _create_connection(address, *args, **kwargs)


socket.socket.connect = _guarded_connect
socket.create_connection = _guarded_create_connection
