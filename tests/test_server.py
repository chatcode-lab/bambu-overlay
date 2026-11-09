"""Offline tests for state parsing and the read-only guarantees.  Run: python3 -m unittest -v"""
import base64
import hashlib
import json
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server  # noqa: E402


def packed(cur, tgt):
    return cur | (tgt << 16)


def tray(i, color, sub="", remain=-1, ttype="PLA"):
    return {"id": str(i), "tray_type": ttype, "tray_sub_brands": sub, "tray_color": color + "FF",
            "cols": [color + "FF"], "remain": remain}


# Shape mirrors a real X2D push_status (trimmed).
REPORT = {
    "command": "push_status", "gcode_state": "RUNNING", "mc_percent": 42, "mc_remaining_time": 83,
    "layer_num": 120, "total_layer_num": 326, "stg_cur": 0, "stg": [29, 2, 13, 7, 1],
    "subtask_name": "Double_Stroller(2)_1", "task_id": "1418163028", "plate_idx": 1, "plate_cnt": 4,
    "gcode_file": "/data/Metadata/plate_1.gcode", "design_id": "835950", "spd_lvl": 2, "spd_mag": 100,
    "mapping": [65535, 65535, 65535, 65535, 2], "print_error": 0, "cooling_fan_speed": "15",
    "nozzle_temper": 220.0, "bed_temper": 55.0,
    "device": {
        "extruder": {"state": 0x112, "info": [
            {"id": 0, "temp": packed(38, 0), "snow": 0xFFFF},
            {"id": 1, "temp": packed(219, 220), "snow": 0x0002}]},
        "nozzle": {"info": [{"id": 0, "diameter": 0.4, "type": "HS01"}, {"id": 1, "diameter": 0.4, "type": "HH01"}]},
        "bed": {"info": {"temp": packed(55, 55)}},
        "ctc": {"info": {"temp": packed(31, 0)}},
    },
    "ams": {"tray_now": "2", "ams": [{"id": "0", "humidity_raw": "43", "temp": "26.1", "tray": [
        tray(0, "F7E6DE", "PLA Basic", 100), tray(1, "FFFFFF", "PLA Pure", 38),
        tray(2, "7C4B00"), tray(3, "F72323")]}]},
    "vir_slot": [{"id": "254", "tray_type": "", "tray_color": "00000000"},
                 {"id": "255", "tray_type": "PLA", "tray_color": "161616FF"}],
}

SLICE_INFO = b"""<?xml version="1.0" encoding="UTF-8"?><config><plate>
<metadata key="index" value="1"/><metadata key="prediction" value="14370"/><metadata key="weight" value="73.48"/>
<object identify_id="1" name="a"/><object identify_id="2" name="b"/>
<filament id="5" type="PLA" color="#7C4B00" used_m="24.25" used_g="73.48"/></plate></config>"""


class HubTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.patches = [mock.patch.object(server, "SETTINGS_FILE", tmp / "settings.json"),
                        mock.patch.object(server, "JOBS_FILE", tmp / "jobs.json"),
                        mock.patch.object(server, "MAKERS_FILE", tmp / "makerworld.json"),
                        mock.patch.object(server, "TWITCH_FILE", tmp / "twitch.json")]
        for p in self.patches:
            p.start()
        cfg = SimpleNamespace(ip="192.168.1.92", access_code="x", serial="20P0TESTSERIAL1",
                              obs_url="", obs_password="", obs_source="X2D")
        self.hub = server.Hub(cfg)
        self.hub.mqtt_ok = True

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def feed(self, **overrides):
        self.hub.on_report({"print": {**json.loads(json.dumps(REPORT)), **overrides}})
        return self.hub.view()


class ViewTests(HubTestCase):
    def test_dual_nozzle_temps_and_active_side(self):
        v = self.feed()
        noz = v["temps"]["nozzles"]
        self.assertEqual([n["side"] for n in noz], ["L", "R"])
        self.assertEqual((noz[0]["cur"], noz[0]["tgt"], noz[0]["active"]), (219, 220, True))
        self.assertEqual((noz[1]["cur"], noz[1]["tgt"], noz[1]["active"]), (38, 0, False))
        self.assertEqual(noz[1]["type"], "Hardened steel")
        self.assertEqual(noz[0]["type"], "Hardened steel · High flow")
        self.assertEqual(v["temps"]["bed"], {"cur": 55, "tgt": 55})
        self.assertEqual(v["temps"]["chamber"], {"cur": 31, "tgt": 0})

    def test_job_fields(self):
        v = self.feed()
        j = v["job"]
        self.assertTrue(v["online"])
        self.assertEqual((j["label"], j["stage"], j["progress"], j["remainingMin"]), ("Printing", None, 42, 83))
        self.assertEqual((j["layer"], j["totalLayers"], j["plate"], j["plateCount"]), (120, 326, 1, 4))
        self.assertEqual(j["makerworld"], "makerworld.com/models/835950")
        self.assertEqual(j["speed"]["name"], "Standard")
        self.assertEqual(v["fans"]["part"], 100)

    def test_purifying_air_after_the_print_shows_as_complete(self):
        with mock.patch.object(server.time, "time", return_value=1_000_000):
            self.feed(gcode_state="PREPARE", mc_percent=0, stg_cur=2)
        with mock.patch.object(server.time, "time", return_value=1_000_000 + 3600):
            j = self.feed(mc_percent=100, layer_num=326, stg_cur=66)["job"]
        self.assertEqual((j["state"], j["label"], j["stage"], j["active"], j["finishing"]),
                         ("FINISH", "Print complete", "Purifying chamber air", False, True))
        self.assertEqual(j["durationSec"], 3600)
        with mock.patch.object(server.time, "time", return_value=1_000_000 + 3600 + 200):
            j = self.feed(gcode_state="FINISH", mc_percent=100, layer_num=326, stg_cur=-1)["job"]
        self.assertEqual((j["state"], j["finishing"], j["durationSec"]), ("FINISH", False, 3600))  # not +purify

    def test_active_tray_and_job_filament_mapping(self):
        self.feed()
        self.hub.set_media(self.hub.job_key, server.parse_slice_info(SLICE_INFO, 1), None)
        v = self.hub.view()
        trays = v["ams"][0]["trays"]
        self.assertEqual([t["active"] for t in trays], [False, False, True, False])
        self.assertEqual(trays[1]["remain"], 38)
        self.assertIsNone(trays[2]["remain"])
        f = v["job"]["filaments"]
        self.assertEqual(len(f), 1)
        self.assertEqual((f[0]["slot"], f[0]["color"], f[0]["usedG"], f[0]["active"]), ("A3", "#7C4B00", 73.5, True))
        self.assertEqual(v["accentSource"], "#7C4B00")
        self.assertEqual([e["slot"] for e in v["external"]], ["Ext-R"])
        self.assertEqual(v["job"]["predictionSec"], 14370)

    def test_two_nozzle_job_lists_both_filaments_with_their_planned_nozzle(self):
        ps = json.dumps({"filament_map": ["1", "2", "1"], "physical_extruder_map": ["1", "0"]}).encode()
        self.assertEqual(server.filament_extruders(ps), {1: 1, 2: 0, 3: 1})   # X2D: 1 = left, 0 = right
        self.assertEqual(server.filament_extruders(b"not json"), {})
        self.feed(mapping=[0, 2, 3])
        self.hub.set_media(self.hub.job_key, {"filaments": [
            {"id": 1, "type": "PLA", "color": "#F7E6DE", "usedG": 12.0, "extruder": 1},
            {"id": 2, "type": "PLA", "color": "#7C4B00", "usedG": 8.5, "extruder": 0},
            {"id": 3, "type": "PETG", "color": "#FFFFFF", "usedG": 0.0, "extruder": 1}]}, None)   # unused: hidden
        f = self.hub.view()["job"]["filaments"]
        self.assertEqual([(x["slot"], x["side"], x["active"]) for x in f], [("A1", "L", False), ("A3", "R", True)])

    def test_job_filament_colour_follows_the_mapped_tray_not_the_sliced_file(self):
        self.feed(mapping=[65535, 65535, 65535, 65535, 3])  # sliced brown, printer feeds red A4
        self.hub.set_media(self.hub.job_key, server.parse_slice_info(SLICE_INFO, 1), None)
        f = self.hub.view()["job"]["filaments"][0]
        self.assertEqual((f["slot"], f["color"]), ("A4", "#F72323"))
        self.feed(mapping=[])  # no mapping (e.g. a re-print): fall back to the sliced colour
        self.assertEqual(self.hub.view()["job"]["filaments"][0]["color"], "#7C4B00")

    def test_prepare_stage_and_steps(self):
        v = self.feed(gcode_state="PREPARE", stg_cur=7, mc_percent=0)
        self.assertEqual(v["job"]["stage"], "Heating hotend")
        self.assertEqual((v["job"]["step"], v["job"]["steps"]), (4, 5))

    def test_idle_has_no_job(self):
        v = self.feed(gcode_state="IDLE", subtask_name="")
        self.assertFalse(v["job"]["hasJob"])
        self.assertIsNone(self.hub.job_key)

    def test_non_status_messages_ignored(self):
        self.hub.on_report({"print": {"command": "gcode_line", "param": "G28"}})
        self.assertEqual(self.hub.report, {})

    def test_settings_validation(self):
        self.assertEqual(self.hub.update_settings({"layout": "hud", "scale": 1.2})["layout"], "hud")
        for bad in ({"layout": "nope"}, {"scale": 9}, {"accent": "red"}, {"unknown": 1}):
            with self.assertRaises(ValueError):
                self.hub.update_settings(bad)


class JobStartTests(HubTestCase):
    def test_new_job_name_can_arrive_before_its_start(self):
        # what happened at 10:27:17: the new job's name came while the printer still reported the previous FINISH
        self.feed(gcode_state="FINISH", mc_percent=100)
        self.feed(gcode_state="FINISH", subtask_name="Key", task_id="2", mc_percent=100)
        self.assertIsNone(self.hub.wanted_media()["start"])  # a fetch now cannot tell an old copy from a new one
        self.feed(gcode_state="PREPARE", subtask_name="Key", task_id="2", mc_percent=0)
        want = self.hub.wanted_media()
        self.assertEqual((want["src"], want["state"]), ("observed", "PREPARE"))
        self.assertEqual(server.recheck_delay({"fileTime": want["start"] - 12 * 3600}, want, want["start"] + 1), 15)


def mw_design(i, handle, originals=()):
    return {"id": i, "designCreator": {"uid": i * 10, "name": handle.title(), "handle": handle}, "originals": list(originals)}


def mw_original(i, handle):
    return {"link": f"https://makerworld.com/models/{i}", "author": handle.title(), "designId": i,
            "homepage": f"https://makerworld.com/@{handle}",
            "insideOriginalInfo": {"designId": i, "designCreator": {"name": handle.title(), "handle": handle}}}


class MakerCreditTests(HubTestCase):
    def test_original_design_credits_its_creator(self):
        info = server.resolve_maker(1, {1: mw_design(1, "kevtoeg")}.__getitem__)
        self.assertEqual((info["maker"]["handle"], info["remix"]), ("kevtoeg", False))

    def test_remix_of_a_remix_credits_the_first_maker(self):
        designs = {3: mw_design(3, "remixer2", [mw_original(2, "remixer1")]),
                   2: mw_design(2, "remixer1", [mw_original(1, "creator")]),
                   1: mw_design(1, "creator")}
        info = server.resolve_maker(3, designs.__getitem__)
        self.assertEqual((info["maker"]["handle"], info["uploader"]["handle"], info["remix"]), ("creator", "remixer2", True))

    def test_lookup_failure_mid_chain_keeps_the_credit_found_so_far(self):
        def fetch(i):
            if i == 2:
                raise OSError("timeout")
            return mw_design(3, "remixer", [mw_original(2, "creator")])
        self.assertEqual(server.resolve_maker(3, fetch)["maker"]["handle"], "creator")

    def test_original_hosted_elsewhere(self):
        named = mw_design(5, "porter", [{"author": "Jane Doe", "designId": 0, "homepage": "", "insideOriginalInfo": None}])
        url_only = mw_design(6, "porter", [{"author": "https://sketchfab.com/3d-models/x", "designId": 0, "homepage": ""}])
        self.assertEqual(server.resolve_maker(5, lambda i: named)["maker"], {"handle": "", "name": "Jane Doe"})
        self.assertEqual(server.resolve_maker(6, lambda i: url_only)["maker"]["handle"], "porter")  # no better name

    def test_view_shows_the_original_maker_else_the_3mf_designer(self):
        self.feed(design_id="835950")
        self.hub.set_media(self.hub.job_key, {"designer": "Uploader Name"}, None)
        self.assertEqual(self.hub.view()["job"]["maker"], {"handle": "", "name": "Uploader Name", "remix": False})
        self.hub.set_maker("835950", {"maker": {"handle": "creator", "name": "Creator"}, "remix": True})
        self.assertEqual(self.hub.view()["job"]["maker"], {"handle": "creator", "name": "Creator", "remix": True})
        self.assertIn("835950", server.load_json(server.MAKERS_FILE, {}))


class FakeTwitch:
    """Stands in for http_json: token endpoint + Get Streams."""
    def __init__(self, viewers=None, token_error=None):
        self.viewers, self.token_error, self.calls, self.expired = viewers, token_error, [], set()

    def __call__(self, url, data=None, headers=None, timeout=10):
        self.calls.append((url, data, headers))
        if url == server.TWITCH_TOKEN:
            if self.token_error:
                raise server.urllib.error.HTTPError(url, self.token_error, "x", {}, None)
            return {"access_token": f"tok{len(self.calls)}", "expires_in": 5000000, "token_type": "bearer"}
        token = headers["Authorization"].split()[-1]
        if token in self.expired:
            raise server.urllib.error.HTTPError(url, 401, "x", {}, None)
        return {"data": [{"type": "live", "viewer_count": self.viewers}] if self.viewers is not None else []}


class TwitchTests(HubTestCase):
    ID, SECRET = "a" * 30, "b" * 30

    def setup_twitch(self):
        self.hub.update_settings({"twitchChannel": "MyChannel"})
        self.hub.set_twitch_creds(self.ID, self.SECRET)

    def test_not_set_up_sleeps_until_woken(self):
        self.assertEqual(server.TwitchViewers(self.hub, FakeTwitch()).poll(), 3600)
        self.assertEqual(self.hub.view()["twitch"]["status"], "not set up")

    def test_live_viewer_count(self):
        self.setup_twitch()
        http = FakeTwitch(viewers=42)
        self.assertEqual(server.TwitchViewers(self.hub, http).poll(), server.TWITCH_POLL)
        t = self.hub.view()["twitch"]
        self.assertEqual((t["status"], t["live"], t["viewers"]), ("live", True, 42))
        self.assertTrue(http.calls[1][0].endswith("user_login=mychannel"))
        self.assertEqual(http.calls[1][2]["Client-Id"], self.ID)

    def test_offline_channel(self):
        self.setup_twitch()
        server.TwitchViewers(self.hub, FakeTwitch(viewers=None)).poll()
        t = self.hub.view()["twitch"]
        self.assertEqual((t["status"], t["live"], t["viewers"]), ("offline", False, None))

    def test_expired_token_is_replaced_once(self):
        self.setup_twitch()
        http = FakeTwitch(viewers=7)
        tw = server.TwitchViewers(self.hub, http)
        tw.poll()
        http.expired.add(tw.token[1])
        tw.poll()
        self.assertEqual([c[0] == server.TWITCH_TOKEN for c in http.calls], [True, False, False, True, False])
        self.assertEqual(self.hub.view()["twitch"]["viewers"], 7)

    def test_rejected_credentials(self):
        self.setup_twitch()
        server.TwitchViewers(self.hub, FakeTwitch(token_error=403)).poll()
        self.assertIn("refused", self.hub.view()["twitch"]["status"])

    def test_secret_is_stored_owner_only_and_never_in_the_view(self):
        self.setup_twitch()
        self.assertEqual(server.TWITCH_FILE.stat().st_mode & 0o777, 0o600)
        self.assertEqual(server.load_twitch_creds(), {"clientId": self.ID, "clientSecret": self.SECRET})
        state = json.dumps(self.hub.view())
        self.assertNotIn(self.SECRET, state)
        self.assertNotIn(self.ID, state)
        self.assertTrue(self.hub.view()["twitch"]["hasSecret"])

    def test_keeping_and_replacing_credentials(self):
        self.setup_twitch()
        self.hub.set_twitch_creds(None, None)   # Save with both fields empty: keep
        self.assertTrue(self.hub.twitch_public()["hasSecret"])
        self.hub.set_twitch_creds("c" * 30, None)   # another app: the old secret no longer applies
        self.assertEqual(self.hub.twitch_public(), {"hasClientId": True, "hasSecret": False, "clientIdTail": "cccc",
                                                    "source": "panel", "envChannel": ""})
        with self.assertRaises(ValueError):
            self.hub.set_twitch_creds("not valid!", None)
        self.hub.set_twitch_creds(None, None, clear=True)
        self.assertFalse(server.TWITCH_FILE.exists())

    def test_env_credentials_win_and_cannot_be_changed_from_the_panel(self):
        cfg = SimpleNamespace(**{**vars(self.hub.cfg), "twitch_client_id": self.ID, "twitch_client_secret": self.SECRET,
                                 "twitch_channel": "envchan"})
        server.save_twitch_creds({"clientId": "c" * 30, "clientSecret": "d" * 30})   # older panel entry
        hub = server.Hub(cfg)
        self.assertEqual(hub.twitch_creds, {"clientId": self.ID, "clientSecret": self.SECRET})
        self.assertEqual((hub.twitch_public()["source"], hub.twitch_public()["envChannel"]), ("env", "envchan"))
        with self.assertRaises(ValueError):
            hub.set_twitch_creds("e" * 30, None)
        http = FakeTwitch(viewers=3)
        server.TwitchViewers(hub, http).poll()   # no channel setting: the .env channel is used
        self.assertTrue(http.calls[1][0].endswith("user_login=envchan"))
        self.assertNotIn(self.SECRET, json.dumps(hub.view()))

    def test_malformed_env_values_are_reported_without_echoing_them(self):
        cfg = SimpleNamespace(**{**vars(self.hub.cfg), "twitch_client_id": '"' + self.ID + '"',
                                 "twitch_client_secret": self.SECRET, "twitch_channel": "envchan"})
        hub = server.Hub(cfg)
        http = FakeTwitch(viewers=3)
        server.TwitchViewers(hub, http).poll()
        status = hub.view()["twitch"]["status"]
        self.assertIn("doesn't look like", status)
        self.assertNotIn(self.ID, status)
        self.assertEqual(http.calls, [])

    def test_channel_setting_is_validated(self):
        with self.assertRaises(ValueError):
            self.hub.update_settings({"twitchChannel": "twitch.tv/me"})

    def test_social_handles(self):
        self.assertTrue(self.hub.settings["socialTwitch"])   # on by default; shown once a channel is set
        self.assertFalse(any(self.hub.settings[k] for k in ("socialTiktok", "socialX", "socialInstagram")))
        s = self.hub.update_settings({"tiktokHandle": "borkasm", "instagramHandle": "bor.ka", "socialX": True})
        self.assertEqual((s["tiktokHandle"], s["instagramHandle"], s["socialX"]), ("borkasm", "bor.ka", True))
        self.assertEqual(self.hub.update_settings({"youtubeHandle": "bor-kasm", "socialYoutube": True})["youtubeHandle"], "bor-kasm")
        self.assertFalse(server.DEFAULT_SETTINGS["socialYoutube"])
        for bad in ("@borkasm", "x.com/karulin", "a b", "<b>"):
            with self.assertRaises(ValueError):
                self.hub.update_settings({"xHandle": bad})
        self.assertEqual(self.hub.settings["socialEvery"], 120)
        self.assertEqual(self.hub.update_settings({"socialEvery": 300})["socialEvery"], 300)
        self.assertEqual(self.hub.update_settings({"socialEvery": 20})["socialEvery"], 20)
        for bad in (10, 5000, 2.5, True, "120"):
            with self.assertRaises(ValueError):
                self.hub.update_settings({"socialEvery": bad})


class CropTargetTests(HubTestCase):
    def test_follows_printer_state(self):
        self.feed()
        self.assertEqual(self.hub.crop_target(), ("printing", [80, 280]))
        self.feed(gcode_state="FINISH")
        self.assertEqual(self.hub.crop_target(), ("idle", [180, 180]))
        self.feed(gcode_state="PREPARE")
        self.assertEqual(self.hub.crop_target()[0], "printing")

    def test_idle_framing_once_the_model_is_done_while_still_running(self):
        # X2D: last layer + 100%, then filament unload and ~3 min "Purifying chamber air" before FINISH
        self.feed(mc_percent=99, layer_num=325)
        self.assertEqual(self.hub.crop_target()[0], "printing")
        self.feed(mc_percent=100, layer_num=326, stg_cur=0)
        self.assertEqual(self.hub.crop_target()[0], "idle")
        self.feed(mc_percent=100, layer_num=326, stg_cur=66)
        self.assertEqual(self.hub.crop_target()[0], "idle")

    def test_holds_when_printer_state_unknown(self):
        self.assertIsNone(self.hub.crop_target())
        self.feed()
        self.hub.mqtt_ok = False
        self.assertIsNone(self.hub.crop_target())

    def test_forced_mode_and_disable(self):
        self.hub.update_settings({"cropMode": "idle", "cropIdle": [150, 210]})
        self.assertEqual(self.hub.crop_target(), ("idle", [150, 210]))
        self.hub.update_settings({"obsCrop": False})
        self.assertIsNone(self.hub.crop_target())
        for bad in ({"cropIdle": [1]}, {"cropIdle": [1.5, 2]}, {"cropPrint": "80,280"}, {"cropMode": "x"}):
            with self.assertRaises(ValueError):
                self.hub.update_settings(bad)

    def test_settings_saved_as_diff_from_defaults(self):
        self.hub.update_settings({"layout": "hud", "credit": False})
        saved = json.loads(server.SETTINGS_FILE.read_text())
        self.assertEqual(saved, {"layout": "hud"})


class FakeObs(threading.Thread):
    """Tiny obs-websocket v5 server: auth + the four requests the crop controller uses."""

    def __init__(self, password):
        super().__init__(daemon=True)
        self.password = password
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.crop = [80, 280]
        self.history = []
        self.muted = {"MugenRadio"}
        self.unmute = False
        self.lock = threading.Lock()

    def _recv(self, conn, buf):
        while len(buf) < 2:
            buf += conn.recv(4096)
        n = buf[1] & 0x7F
        off = 2
        if n == 126:
            n = struct.unpack("!H", bytes(buf[2:4]))[0]
            off = 4
        while len(buf) < off + 4 + n:
            buf += conn.recv(4096)
        mask = buf[off:off + 4]
        data = bytes(b ^ mask[i & 3] for i, b in enumerate(buf[off + 4:off + 4 + n]))
        opcode = buf[0] & 0x0F
        del buf[:off + 4 + n]
        return opcode, data

    def _send(self, conn, obj):
        data = json.dumps(obj).encode()
        head = bytes([0x81, len(data)]) if len(data) < 126 else bytes([0x81, 126]) + struct.pack("!H", len(data))
        with self.lock:
            conn.sendall(head + data)

    def _pump_meters(self, conn):
        meters = {"inputs": [
            {"inputName": "MugenRadio", "inputLevelsMul": [[0.2, 0.5, 0.5], [0.1, 0.25, 0.25]]},
            {"inputName": "Mic", "inputLevelsMul": [[0.05, 0.8, 0.8]]},          # mono
            {"inputName": "Camera", "inputLevelsMul": []}]}                       # no audio
        try:
            while True:
                if self.unmute and "MugenRadio" in self.muted:
                    self.muted.discard("MugenRadio")
                    self._send(conn, {"op": 5, "d": {"eventType": "InputMuteStateChanged", "eventIntent": 8,
                                                     "eventData": {"inputName": "MugenRadio", "inputMuted": False}}})
                self._send(conn, {"op": 5, "d": {"eventType": "InputVolumeMeters", "eventIntent": 65536,
                                                 "eventData": meters}})
                time.sleep(0.02)
        except OSError:
            return

    def run(self):
        conn, _ = self.srv.accept()
        req = b""
        while b"\r\n\r\n" not in req:
            req += conn.recv(4096)
        key = [l.split(b":", 1)[1].strip() for l in req.split(b"\r\n") if l.lower().startswith(b"sec-websocket-key")][0]
        accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")
        salt, challenge = "c2FsdA==", "Y2hhbGxlbmdl"
        self._send(conn, {"op": 0, "d": {"rpcVersion": 1, "authentication": {"salt": salt, "challenge": challenge}}})
        buf = bytearray()
        _, data = self._recv(conn, buf)
        ident = json.loads(data)["d"]
        secret = base64.b64encode(hashlib.sha256((self.password + salt).encode()).digest()).decode()
        expected = base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()
        if ident.get("authentication") != expected:
            conn.sendall(bytes([0x88, 2]) + struct.pack("!H", 4009))
            conn.close()
            return
        self._send(conn, {"op": 2, "d": {"negotiatedRpcVersion": 1}})
        if ident.get("eventSubscriptions", 0) & (1 << 16):  # meter client: stream events alongside requests
            threading.Thread(target=self._pump_meters, args=(conn,), daemon=True).start()
        while True:
            try:
                opcode, data = self._recv(conn, buf)
            except OSError:
                return
            if opcode == 8:
                conn.close()
                return
            d = json.loads(data)["d"]
            rt, rd = d["requestType"], d.get("requestData") or {}
            out = {}
            if rt == "GetSceneList":
                out = {"scenes": [{"sceneName": "Scene"}]}
            elif rt == "GetSceneItemList":
                out = {"sceneItems": [{"sourceName": "X2D", "sceneItemId": 1, "isGroup": False},
                                      {"sourceName": "Browser", "sceneItemId": 4, "isGroup": False}]}
            elif rt == "GetSceneItemTransform":
                out = {"sceneItemTransform": {"cropTop": self.crop[0], "cropBottom": self.crop[1], "cropLeft": 280}}
            elif rt == "GetInputMute":
                out = {"inputMuted": rd["inputName"] in self.muted}
            elif rt == "SetSceneItemTransform":
                assert rd["sceneItemId"] == 1 and set(rd["sceneItemTransform"]) == {"cropTop", "cropBottom"}
                self.crop = [rd["sceneItemTransform"]["cropTop"], rd["sceneItemTransform"]["cropBottom"]]
                self.history.append(list(self.crop))
            self._send(conn, {"op": 7, "d": {"requestType": rt, "requestId": d["requestId"],
                                             "requestStatus": {"result": True, "code": 100}, "responseData": out}})


class ObsCropTests(HubTestCase):
    def test_smooth_crop_transition_against_fake_obs(self):
        fake = FakeObs("hunter2")
        fake.start()
        self.hub.cfg.obs_url, self.hub.cfg.obs_password = f"ws://127.0.0.1:{fake.port}", "hunter2"
        self.hub.update_settings({"cropMode": "idle", "cropDuration": 0.3})
        ctl = server.ObsCropController(self.hub)
        stopper = threading.Timer(0.9, lambda: self.hub.update_settings({"obsCrop": False}))
        stopper.start()
        ctl._session()
        self.assertEqual(fake.crop, [180, 180])
        self.assertGreater(len(fake.history), 5, "transition should be animated, not a jump")
        tops = [h[0] for h in fake.history]
        self.assertEqual(tops, sorted(tops))
        self.assertTrue(all(t + b == 360 for t, b in fake.history))  # pans: visible height stays constant
        self.assertTrue(self.hub.obs["status"].startswith("connected"))

    def test_wrong_password_is_reported(self):
        fake = FakeObs("right")
        fake.start()
        self.hub.cfg.obs_url, self.hub.cfg.obs_password = f"ws://127.0.0.1:{fake.port}", "wrong"
        with self.assertRaises(PermissionError):
            server.ObsCropController(self.hub)._session()


class MusicCreditTests(HubTestCase):
    def test_an_input_counts_as_playing_while_unmuted_and_recently_audible(self):
        last = {}
        loud = [{"inputName": "ltbr.fm", "inputLevelsMul": [[0.2, 0.3, 0.3], [0.2, 0.3, 0.3]]},
                {"inputName": "MugenRadio", "inputLevelsMul": [[0.0, 0.0, 0.0]]},
                {"inputName": "X2D", "inputLevelsMul": [[0.1, 0.2, 0.2]]}]
        server.note_sound(last, loud, 100.0)
        self.assertEqual(server.audible_inputs(last, {"X2D": True}, 101.0), ["ltbr.fm"])   # muted / silent: no
        self.assertEqual(server.audible_inputs(last, {}, 100.0 + server.AUDIBLE_HOLD - 1), ["X2D", "ltbr.fm"])
        self.assertEqual(server.audible_inputs(last, {}, 100.0 + server.AUDIBLE_HOLD + 1), [])   # long silence

    def test_music_credit_settings(self):
        self.assertFalse(self.hub.settings["musicCredit"])
        s = self.hub.update_settings({"musicCredit": True, "musicText": "ltbr.fm", "musicLabel": "Music", "musicInput": "ltbr.fm"})
        self.assertEqual((s["musicText"], s["musicInput"]), ("ltbr.fm", "ltbr.fm"))
        for bad in ({"musicText": "x" * 61}, {"musicLabel": "two\nlines"}, {"musicText": 5}):
            with self.assertRaises(ValueError):
                self.hub.update_settings(bad)


class MeterTests(HubTestCase):
    def test_aggregate_loudest_and_selected(self):
        inputs = [{"inputName": "Music", "inputLevelsMul": [[0.2, 0.5, 0.6], [0.1, 0.3, 0.3]]},
                  {"inputName": "Mic", "inputLevelsMul": [[0.05, 0.8, 0.9]]},
                  {"inputName": "Cam", "inputLevelsMul": []}]
        levels, names = server.aggregate_meter(inputs)
        self.assertEqual(levels, [0.2, 0.1, 0.8, 0.8])   # mono mic feeds both channels' peak
        self.assertEqual(names, ["Mic", "Music"])
        self.assertEqual(server.aggregate_meter(inputs, "Music")[0], [0.2, 0.1, 0.5, 0.3])
        self.assertEqual(server.aggregate_meter(inputs, "", {"Music"})[0], [0.05, 0.05, 0.8, 0.8])
        self.assertEqual(server.aggregate_meter(inputs, "Music", {"Music"})[0], [0.0, 0.0, 0.0, 0.0])
        self.assertEqual(server.aggregate_meter([], "")[0], [0.0, 0.0, 0.0, 0.0])

    def _wait(self, cond, timeout=4.0):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.02)
        return False

    def test_meter_reader_skips_muted_inputs_and_follows_unmute(self):
        fake = FakeObs("pw")
        fake.start()
        self.hub.cfg.obs_url, self.hub.cfg.obs_password = f"ws://127.0.0.1:{fake.port}", "pw"
        reader = threading.Thread(target=server.ObsMeterReader(self.hub)._session, daemon=True)
        reader.start()
        try:
            # MugenRadio (backup) is muted: only the mono mic counts
            self.assertTrue(self._wait(lambda: self.hub.meter == [0.05, 0.05, 0.8, 0.8]), self.hub.meter)
            self.assertEqual(self.hub.obs["meterMuted"], ["MugenRadio"])
            self.assertEqual(self.hub.obs["meterInputs"], ["Mic", "MugenRadio"])
            self.assertEqual(self.hub.obs["meter"], "live")
            fake.unmute = True  # un-muted in OBS -> InputMuteStateChanged
            self.assertTrue(self._wait(lambda: self.hub.meter == [0.2, 0.1, 0.8, 0.8]), self.hub.meter)
            self.assertEqual(self.hub.obs["meterMuted"], [])
        finally:
            self.hub.update_settings({"meter": False})
            reader.join(3)


class ParsingTests(unittest.TestCase):
    def test_slice_info(self):
        info = server.parse_slice_info(SLICE_INFO, 1)
        self.assertEqual(info["weightG"], 73.5)
        self.assertEqual(info["objects"], 2)
        self.assertEqual(info["filaments"][0]["id"], 5)

    def test_pick_model_file_prefers_newest_name_match(self):
        files = [{"name": "Double_Stroller(2)_1.gcode.3mf", "path": "/h/a", "time": 10},
                 {"name": "Double_Stroller(2)_1.gcode.3mf", "path": "/h/b", "time": 20},
                 {"name": "Other.gcode.3mf", "path": "/h/c", "time": 30}]
        self.assertEqual(server.pick_model_file(files, "Double_Stroller(2)_1", "")["path"], "/h/b")
        self.assertIsNone(server.pick_model_file(files, "Missing", "/data/Metadata/plate_1.gcode"))

    def test_pick_model_file_matches_hex_escaped_characters(self):
        files = [{"name": "Pasta Playset3a Pasta Sauce Jar.gcode.3mf", "path": "/h/a", "time": 10},
                 {"name": "AC2fDC Adapter.gcode.3mf", "path": "/h/b", "time": 20}]
        self.assertEqual(server.pick_model_file(files, "Pasta Playset: Pasta Sauce Jar", "")["path"], "/h/a")
        self.assertEqual(server.pick_model_file(files, "AC/DC Adapter", "")["path"], "/h/b")

    def _3mf(self, plate: int) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "job.gcode.3mf"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr(f"Metadata/plate_{plate}.gcode", "; gcode")
            z.writestr(f"Metadata/plate_{plate}.png", f"png-{plate}")
            z.writestr("Metadata/slice_info.config", SLICE_INFO.replace(b'value="1"', f'value="{plate}"'.encode()))
        return path

    def test_parse_3mf_uses_the_job_plate(self):
        meta, cover = server.parse_3mf(self._3mf(3), 3)
        self.assertEqual((meta["plate"], meta["weightG"], cover), (3, 73.5, b"png-3"))
        self.assertEqual(server.parse_3mf(self._3mf(2), None)[0]["plate"], 2)  # plate unknown: the sliced one

    def test_parse_3mf_rejects_another_plates_file(self):
        # same project printed earlier with plate 1; the job now prints plate 3 -> not the job's file (yet)
        with self.assertRaises(server.StaleModelFile):
            server.parse_3mf(self._3mf(1), 3)

    def test_stale_file_reason_waits_for_the_new_copy_only_while_the_job_is_young(self):
        start = 1_000_000
        want = {"start": start, "src": "observed"}
        old, new = {"name": "a.gcode.3mf", "time": start - 11 * 3600}, {"name": "a.gcode.3mf", "time": start + 4}
        self.assertIsNotNone(server.stale_file_reason(old, want, start + 1))
        self.assertIsNone(server.stale_file_reason(new, want, start + 10))
        self.assertIsNone(server.stale_file_reason(old, want, start + server.FRESH_FILE_GRACE + 1))  # re-print
        self.assertIsNone(server.stale_file_reason(old, {"start": start, "src": "first-seen"}, start + 1))

    def test_recheck_delay_only_while_a_young_job_holds_an_older_file(self):
        start = 1_000_000
        want = {"start": start, "src": "observed", "state": "PREPARE"}
        old, new = {"fileTime": start - 12 * 3600}, {"fileTime": start + 5}
        self.assertEqual(server.recheck_delay(old, want, start + 1), 15)
        self.assertEqual(server.recheck_delay(old, want, start + server.FRESH_FILE_GRACE + 1), 120)
        self.assertIsNone(server.recheck_delay(old, want, start + server.RECHECK_FOR + 1))
        self.assertIsNone(server.recheck_delay(new, want, start + 1))
        self.assertIsNone(server.recheck_delay(old, {**want, "state": "FINISH"}, start + 1))
        self.assertIsNone(server.recheck_delay(old, {**want, "src": "first-seen"}, start + 1))

    def test_recheck_replaces_a_copy_from_an_earlier_print(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        start, blob = 1_000_000, self._3mf(1).read_bytes()
        listing = [{"name": "Key.gcode.3mf", "path": "/h/Key.gcode.3mf", "time": start - 12 * 3600}]

        class FakeChannel:
            def __init__(self, *a): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def storages(self): return ["emmc"]
            def list_models(self, storage): return listing
            def download(self, path, fh): fh.write(blob)

        hub = mock.Mock()
        want = {"key": "k", "name": "Key", "gcode_file": "", "plate": 1, "state": "PREPARE",
                "start": start, "src": "observed"}
        with mock.patch.object(server, "MEDIA_DIR", Path(tmp.name)), \
                mock.patch.object(server, "CACHE_DIR", Path(tmp.name)), \
                mock.patch.object(server, "ReadOnlyFileChannel", FakeChannel), \
                mock.patch.object(server.time, "time", return_value=start + 2):
            worker = server.MediaWorker(hub)
            worker._recheck(want, {"fileTime": start - 12 * 3600})  # printer has not saved this job's copy yet
            hub.set_media.assert_not_called()
            worker._recheck(want, {"fileTime": start - 12 * 3600})  # too soon: no second listing
            listing[0]["time"] = start + 5
            worker.next_recheck = ("k", 0.0)
            worker._recheck(want, {"fileTime": start - 12 * 3600})
        hub.set_media.assert_called_once()
        self.assertEqual(hub.set_media.call_args.args[1]["fileTime"], start + 5)

    def test_job_plate(self):
        self.assertEqual(server.job_plate({"plate_idx": 3}), 3)
        self.assertEqual(server.job_plate({"gcode_file": "/data/Metadata/plate_4.gcode"}), 4)
        self.assertIsNone(server.job_plate({}))

    def test_media_cache_ignores_entries_from_older_versions(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        hub = mock.Mock()
        with mock.patch.object(server, "MEDIA_DIR", Path(tmp.name)):
            worker = server.MediaWorker(hub)
            d = worker._dir("k")
            d.mkdir(parents=True)
            server.save_json(d / "meta.json", {"weightG": 13.6})
            self.assertFalse(worker._load_cached({"key": "k"}))
            server.save_json(d / "meta.json", {"weightG": 15.2, "v": server.MEDIA_CACHE_V})
            self.assertTrue(worker._load_cached({"key": "k"}))
        hub.set_media.assert_called_once_with("k", {"weightG": 15.2, "v": server.MEDIA_CACHE_V}, None)

    def test_config_without_printer_allows_demo_only(self):
        env = {k: v for k, v in server.os.environ.items() if not k.startswith(("BAMBU_", "OVERLAY_", "OBS_"))}
        with mock.patch.object(server, "load_env", return_value={}), mock.patch.dict(server.os.environ, env, clear=True):
            cfg = server.Config()  # used to sys.exit()
        self.assertFalse(cfg.has_printer)
        self.assertEqual((cfg.host, cfg.port), ("0.0.0.0", 8787))

    def test_cert_cn_picks_subject_not_issuer(self):
        der = (b"\x30\x00" + b"\x06\x03\x55\x04\x03\x0c\x13BBL Device CA N6-V2"
               + b"\x30\x00" + b"\x06\x03\x55\x04\x03\x0c\x0f20P0TESTSERIAL1")
        self.assertEqual(server._cert_subject_cn(der), "20P0TESTSERIAL1")

    def test_file_frame_split_strips_separator(self):
        reply, binary = server.ReadOnlyFileChannel._split(b'{"result":1,"reply":{"offset":0}}\n\nPK\x03\x04')
        self.assertEqual(reply["result"], 1)
        self.assertEqual(binary, b"PK\x03\x04")


class ReadOnlyGuaranteeTests(unittest.TestCase):
    def test_file_channel_rejects_non_read_commands(self):
        fc = server.ReadOnlyFileChannel("0.0.0.0", "", "")
        for cmd in (2, 3, 5, 6, 8):  # sub-file, delete, upload, ...
            with self.assertRaises(PermissionError):
                fc._request(cmd, {})

    def test_mqtt_cannot_publish(self):
        client = server.ReadOnlyMqtt.__new__(server.ReadOnlyMqtt)
        client.sock = mock.Mock()
        with self.assertRaises(PermissionError):
            client._send(0x30, b"\x00\x05topic{}")  # PUBLISH
        client.sock.sendall.assert_not_called()
        self.assertNotIn(0x3, server.ReadOnlyMqtt._ALLOWED_OUT)


if __name__ == "__main__":
    unittest.main()
