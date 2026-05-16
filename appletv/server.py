"""
Apple TV remote — backend.

A browser cannot speak Apple TV's encrypted Companion/AirPlay protocols, so this
small server does it on your behalf using `pyatv`. Run it on any computer or
Raspberry Pi that stays on your home Wi-Fi, then open the printed URL on your
iPhone (and "Add to Home Screen" for a full-screen remote).

Setup:
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
    python server.py

First run: open the page on your iPhone, tap the gear, "Scan", then pair.
The Apple TV shows a 4-digit PIN — type it in. Credentials are saved to
appletv_creds.json so you only pair once.
"""

import asyncio
import threading
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

import pyatv
from pyatv.const import Protocol
from pyatv.storage.file_storage import FileStorage

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
CREDS_FILE = str(BASE_DIR / "appletv_creds.json")

# Protocols paired for a modern Apple TV: Companion drives the remote/power,
# AirPlay exposes now-playing metadata and artwork.
PAIR_PROTOCOLS = [Protocol.Companion, Protocol.AirPlay]


class AppleTVManager:
    """Owns a dedicated asyncio loop (Flask is sync) and one live connection."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self._run_loop, daemon=True).start()
        self.storage = self._run(self._init_storage())
        self.atv = None
        self.config = None
        self.pairing = None
        self.pairing_proto = None

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _run(self, coro):
        """Run a coroutine on the background loop and block for the result."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    async def _init_storage(self):
        storage = FileStorage(CREDS_FILE, self.loop)
        await storage.load()
        return storage

    # --- discovery / pairing ---------------------------------------------

    async def _scan(self):
        results = await pyatv.scan(self.loop, timeout=5, storage=self.storage)
        devices = []
        for conf in results:
            paired = [
                s.protocol.name
                for s in conf.services
                if s.credentials is not None
            ]
            devices.append(
                {
                    "name": conf.name,
                    "identifier": conf.identifier,
                    "address": str(conf.address),
                    "paired": paired,
                }
            )
        return devices

    async def _find(self, identifier):
        results = await pyatv.scan(
            self.loop, timeout=5, identifier=identifier, storage=self.storage
        )
        if not results:
            raise RuntimeError("Device not found on the network")
        return results[0]

    async def _pair_begin(self, identifier):
        self.config = await self._find(identifier)
        # Pair only protocols that still need credentials.
        self._todo = [
            p
            for p in PAIR_PROTOCOLS
            if self.config.get_service(p) is not None
            and self.config.get_service(p).credentials is None
        ]
        return await self._pair_next()

    async def _pair_next(self):
        if self.pairing is not None:
            try:
                await self.pairing.close()
            except Exception:
                pass
            self.pairing = None
        if not self._todo:
            await self.storage.save()
            return {"done": True}
        self.pairing_proto = self._todo[0]
        self.pairing = await pyatv.pair(
            self.config, self.pairing_proto, self.loop, storage=self.storage
        )
        await self.pairing.begin()
        return {
            "done": False,
            "protocol": self.pairing_proto.name,
            "needs_pin": self.pairing.device_provides_pin,
        }

    async def _pair_pin(self, pin):
        if self.pairing is None:
            raise RuntimeError("No pairing in progress")
        self.pairing.pin(pin)
        await self.pairing.finish()
        if not self.pairing.has_paired:
            raise RuntimeError("Pairing failed — check the PIN and try again")
        self._todo.pop(0)
        return await self._pair_next()

    # --- connection / control --------------------------------------------

    async def _ensure_connected(self):
        if self.atv is not None:
            return
        results = await pyatv.scan(self.loop, timeout=5, storage=self.storage)
        candidates = [
            c
            for c in results
            if any(s.credentials is not None for s in c.services)
        ]
        if not candidates:
            raise RuntimeError("No paired Apple TV found. Pair one first.")
        self.atv = await pyatv.connect(
            candidates[0], self.loop, storage=self.storage
        )

    async def _command(self, name):
        await self._ensure_connected()
        rc = self.atv.remote_control
        power_map = {
            "turn_on": self.atv.power.turn_on,
            "turn_off": self.atv.power.turn_off,
        }
        if name in power_map:
            await power_map[name]()
            return
        action = getattr(rc, name, None)
        if action is None:
            raise RuntimeError(f"Unknown command: {name}")
        await action()

    async def _now_playing(self):
        await self._ensure_connected()
        try:
            playing = await self.atv.metadata.playing()
        except Exception:
            return {"available": False}
        app = getattr(self.atv.metadata, "app", None)
        return {
            "available": True,
            "title": playing.title,
            "artist": playing.artist,
            "album": playing.album,
            "state": playing.device_state.name if playing.device_state else None,
            "app": app.name if app else None,
        }

    async def _disconnect(self):
        if self.atv is not None:
            self.atv.close()
            self.atv = None

    # --- sync wrappers used by Flask -------------------------------------

    def scan(self):
        return self._run(self._scan())

    def pair_begin(self, identifier):
        return self._run(self._pair_begin(identifier))

    def pair_pin(self, pin):
        return self._run(self._pair_pin(pin))

    def command(self, name):
        return self._run(self._command(name))

    def now_playing(self):
        return self._run(self._now_playing())

    def disconnect(self):
        return self._run(self._disconnect())


manager = AppleTVManager()
app = Flask(__name__, static_folder=None)


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/<path:path>")
def static_files(path):
    return send_from_directory(STATIC_DIR, path)


@app.route("/api/scan", methods=["POST"])
def api_scan():
    try:
        return jsonify({"ok": True, "devices": manager.scan()})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/pair/begin", methods=["POST"])
def api_pair_begin():
    identifier = (request.get_json(silent=True) or {}).get("identifier")
    if not identifier:
        return jsonify({"ok": False, "error": "identifier required"}), 400
    try:
        return jsonify({"ok": True, **manager.pair_begin(identifier)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/pair/pin", methods=["POST"])
def api_pair_pin():
    pin = (request.get_json(silent=True) or {}).get("pin")
    if not pin:
        return jsonify({"ok": False, "error": "pin required"}), 400
    try:
        return jsonify({"ok": True, **manager.pair_pin(str(pin))})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/command/<name>", methods=["POST"])
def api_command(name):
    try:
        manager.command(name)
        return jsonify({"ok": True})
    except Exception as exc:
        manager.disconnect()  # force reconnect on next call
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/nowplaying")
def api_now_playing():
    try:
        return jsonify({"ok": True, **manager.now_playing()})
    except Exception as exc:
        manager.disconnect()
        return jsonify({"ok": False, "error": str(exc)}), 500


if __name__ == "__main__":
    import socket

    host_ip = socket.gethostbyname(socket.gethostname())
    print("\n  Apple TV remote running.")
    print(f"  On your iPhone (same Wi-Fi) open:  http://{host_ip}:8765\n")
    app.run(host="0.0.0.0", port=8765, threaded=True)
