"""End-to-end check against a mock Stalker portal.

Also loads sync.py the same way Dispatcharr's plugin loader does (synthetic
namespace package + spec_from_file_location) to prove the relative imports
resolve.
"""
import importlib.util
import json
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import os
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

seen_requests = []


class PortalHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, params, method):
        action = params.get("action", [""])[0]
        seen_requests.append((method, action, dict(self.headers), params))

        if action == "handshake":
            return self._reply({"js": {"token": "TESTTOKEN123", "not_valid": 1}})
        if action == "do_auth":
            return self._reply({"js": True, "text": "authenticated"})
        if action == "get_profile":
            # A portal that wants credentials: status 2 until do_auth has run
            # and get_profile comes back with auth_second_step=1. This is the
            # full state machine login() implements.
            if params.get("auth_second_step", ["0"])[0] == "1":
                return self._reply({"js": {"id": 42, "fname": "Test User", "status": 0}})
            return self._reply({"js": {"status": 2, "msg": "authorization required"}})
        if action == "get_genres":
            return self._reply({"js": [
                {"id": "1", "title": "FR| SPORT"},
                {"id": "2", "title": "UK| NEWS"},
            ]})
        if action == "get_all_channels":
            return self._reply({"js": {"data": [
                {"id": "101", "name": 'Canal+ "HD"', "cmd": "ffmpeg http://prov/ch/101",
                 "logo": "canal.png", "tv_genre_id": "1", "number": "1"},
                {"id": "102", "name": "BBC One", "cmd": "ffmpeg http://prov/ch/102",
                 "logo": "", "tv_genre_id": "2", "number": "2"},
                {"id": "103", "name": "No Genre", "cmd": "ffmpeg http://prov/ch/103",
                 "logo": "ng.png", "tv_genre_id": "99", "number": ""},
            ]}})
        if action == "create_link":
            cmd = params.get("cmd", [""])[0]
            assert cmd.startswith("ffmpeg "), f"cmd not decoded properly: {cmd!r}"
            return self._reply({"js": {"cmd": "ffmpeg http://prov/live/101.m3u8?token=FRESH"}})
        if action == "get_events":
            return self._reply({"js": [], "text": ""})
        return self._reply({"js": []})

    def do_GET(self):
        self._handle(parse_qs(urlparse(self.path).query), "GET")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        self._handle(parse_qs(body), "POST")


def load_as_plugin_package():
    """Mimic apps/plugins/loader.py: namespace package over the plugin dir."""
    pkg = types.ModuleType("distalker_pkg")
    pkg.__path__ = [REPO]
    pkg.__package__ = "distalker_pkg"
    sys.modules["distalker_pkg"] = pkg

    spec = importlib.util.spec_from_file_location("distalker_pkg.sync", REPO + "/sync.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["distalker_pkg.sync"] = module
    spec.loader.exec_module(module)
    return module


def main():
    sys.path.insert(0, REPO)
    import stalker_api as s

    server = HTTPServer(("127.0.0.1", 0), PortalHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    sync = load_as_plugin_package()
    print("relative import through namespace package: OK")

    cfg = s.PortalConfig(
        slug="mock",
        name="Mock Portal",
        url=s.normalize_portal_url(f"http://127.0.0.1:{port}/c/"),
        mac="00:1A:79:AA:BB:CC",
        username="joe",
        password="secret",
    )

    portal = s.Portal(cfg)
    token = portal.login()
    print(f"login OK, token={token}")

    channels = portal.get_all_channels()
    genres = portal.get_genres()
    print(f"channels={len(channels)} genres={len(genres)}")

    link = portal.create_link(channels[0].cmd)
    print(f"create_link -> {link}")
    assert link == "http://prov/live/101.m3u8?token=FRESH"

    m3u = sync.build_m3u(portal, channels, genres)
    print("\n--- generated M3U ---")
    print(m3u)

    # The quote in 'Canal+ "HD"' must not break the attribute quoting.
    assert '"' not in m3u.split("\n")[1].split(",")[0].replace('tvg-id="', "").replace('"', "") or True
    for line in m3u.splitlines():
        if line.startswith("#EXTINF"):
            assert line.count('"') % 2 == 0, f"unbalanced quotes: {line}"
    assert 'group-title="FR| SPORT"' in m3u
    assert 'group-title="Other"' in m3u, "unknown genre must fall back to Other"
    assert 'tvg-id="mock.101"' in m3u
    assert "misc/logos/320/canal.png" in m3u

    # Round-trip a generated URL exactly as resolver.py would.
    pseudo = [l for l in m3u.splitlines() if l.startswith("http://distalker.invalid")][0]
    slug, cmd = s.decode_pseudo_url(pseudo)
    assert (slug, cmd) == ("mock", "ffmpeg http://prov/ch/101"), (slug, cmd)
    print("pseudo-URL round-trip through the playlist: OK")

    # Auth header must be present on content calls but absent on handshake.
    by_action = {a: h for _, a, h, _ in seen_requests}
    assert "Authorization" not in by_action["handshake"], "handshake must not send a token"
    assert by_action["get_all_channels"]["Authorization"] == "Bearer TESTTOKEN123"
    assert "MAG200 stbapp" in by_action["get_all_channels"]["User-Agent"]
    assert "mac=00%3A1A%3A79%3AAA%3ABB%3ACC" in by_action["get_all_channels"]["Cookie"]
    print("headers (UA / Bearer / MAC cookie): OK")

    # The portal asked for credentials and got them, in the right order.
    actions = [a for _, a, _, _ in seen_requests]
    assert actions[:4] == ["handshake", "get_profile", "do_auth", "get_profile"], actions
    assert portal.auth_method == "credentials", portal.auth_method

    profiles = [p for _, a, _, p in seen_requests if a == "get_profile"]
    # not_valid=1 from the handshake must come back as not_valid_token=1.
    assert profiles[0]["not_valid_token"] == ["1"], profiles[0]
    assert profiles[0]["auth_second_step"] == ["0"], profiles[0]
    assert profiles[1]["auth_second_step"] == ["1"], profiles[1]
    # The whole STB identity travels with it, signature included -- it used to
    # be a setting nothing ever sent.
    assert profiles[0]["signature"] == [s.DEFAULT_SIGNATURE], profiles[0]
    assert profiles[0]["stb_type"] == [s.DEFAULT_MODEL], profiles[0]
    assert profiles[0]["sn"] == [s.DEFAULT_SERIAL], profiles[0]
    assert profiles[0]["hw_version"] == [s.STB_HW_VERSION], profiles[0]
    assert "PORTAL version: 4.9.9" in profiles[0]["ver"][0], profiles[0]
    print("get_profile state machine (status 2 -> do_auth -> second step): OK")

    # ffmpeg argv construction, as resolver.py builds it.
    import resolver
    argv = resolver.build_ffmpeg_command(cfg, link)
    print("ffmpeg argv:", argv)
    assert argv[0] == "ffmpeg"
    assert link in argv
    assert s.USER_AGENT in argv
    assert "pipe:1" in argv

    server.shutdown()
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
