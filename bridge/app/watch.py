"""Background watches: favourite repeaters' batteries, and favourites coming back
into range. Alerts go out as Web Push (and an SSE `alert` event)."""
import asyncio
import logging
import time

from meshcore import EventType

log = logging.getLogger("watch")

BATTERY_SECS = 3 * 3600        # how often to ask each watched repeater for its status
BATTERY_FIRST_SECS = 120       # first check this long after the radio connects
BATTERY_ALERT_EVERY = 24 * 3600
BACK_AFTER_HOURS = 6           # silent this long, then heard again = "back in range"
STARTUP_QUIET_SECS = 600       # don't alert while the bridge itself has just (re)started
OFFLINE_ALERT_SECS = 300       # radio unreachable this long = alert (e.g. its power bank died)
OTA_TIMEOUT = 20               # login/status over the mesh: floods can take a while


def cell_pct(v: float) -> int:
    """Rough single-cell Li-ion resting voltage -> % (same table as the PWA).

    Most repeaters (and many companions) run 18650s, NMC/NCA, often several in
    parallel, which reads the same as one cell. Their curve sits lower and
    flatter mid-range than the RC LiPo table this replaced, which read about
    20 points low around 3.8 V. Measured while the radio listens, so a ballpark.
    """
    t = [(3.00, 0), (3.30, 3), (3.45, 10), (3.57, 20), (3.63, 30), (3.68, 40), (3.73, 50),
         (3.80, 60), (3.87, 70), (3.95, 80), (4.05, 90), (4.18, 100)]
    if v <= t[0][0]:
        return 0
    for (v0, p0), (v1, p1) in zip(t, t[1:]):
        if v <= v1:
            return round(p0 + (p1 - p0) * (v - v0) / (v1 - v0))
    return 100


class Watch:
    def __init__(self, radio, db, hub):
        self.radio, self.db, self.hub = radio, db, hub
        self.started = time.time()
        self.seen: dict[str, float] = db.get_setting("fav_seen", {}) or {}
        # every contact's last sign of life, for the "last heard" dots: {pubkey: [epoch s, how]}
        self.last: dict[str, list] = db.get_setting("last_heard", {}) or {}
        for pk, ts in db.last_heard_seed().items():   # adverts + DMs already stored
            if ts / 1000 > (self.last.get(pk) or [0])[0]:
                self.last[pk] = [ts / 1000, "heard earlier"]
        self._dirty = False
        self.offline_alerted = False

    # ---------- back in range ----------
    def _fav_contacts(self) -> list[dict]:
        favs = set(self.db.get_favourites())
        mc = self.radio.mc
        return [c for k, c in (mc.contacts.items() if mc else []) if k in favs]

    async def heard(self, *, key: str | None = None, name: str | None = None, hop: str | None = None,
                    how: str = "", snr=None):
        """Something from a contact reached us. Match by full key, by name
        (channel posts carry only a name) or by 1-byte hop (a repeater relaying).
        Every contact's last-heard time is kept; favourites also get back-in-range alerts."""
        mc = self.radio.mc
        for pk, c in (mc.contacts.items() if mc else []):
            if (key and pk.startswith(key)) or (name and (c.get("adv_name") or "") == name) \
                    or (hop and c.get("type") == 2 and pk.startswith(hop)):
                self.last[pk] = [time.time(), how]
                self._dirty = True
        for c in self._fav_contacts():
            pk = c["public_key"]
            if key and not pk.startswith(key):
                continue
            if name and (c.get("adv_name") or "") != name:
                continue
            if hop and not (c.get("type") == 2 and pk.startswith(hop)):
                continue
            if not (key or name or hop):
                continue
            await self._mark(pk, c.get("adv_name") or pk[:8], how, snr)

    async def _mark(self, pk: str, name: str, how: str, snr):
        now = time.time()
        prev = self.seen.get(pk)
        self.seen[pk] = now
        self._dirty = True
        hours = float(self.db.get_setting("back_after_hours", BACK_AFTER_HOURS) or BACK_AFTER_HOURS)
        if (prev and now - prev > hours * 3600 and now - self.started > STARTUP_QUIET_SECS
                and self.db.get_setting("notify_back", True)):
            gap = (now - prev) / 3600
            gap_txt = f"{gap:.0f} h" if gap < 48 else f"{gap / 24:.0f} days"
            body = f"Heard again after {gap_txt}" + (f" ({how}" + (f", {snr} dB" if snr is not None else "") + ")" if how else "")
            log.info("back in range: %s (%s)", name, body)
            await self.hub.alert(f"{name} is back in range", body, f"back:{pk[:12]}", f"/#/contact/{pk[:12]}")

    def flush(self):
        if self._dirty:
            self.db.set_setting("fav_seen", self.seen)
            self.db.set_setting("last_heard", self.last)
            self._dirty = False

    # ---------- our own radio going offline ----------
    async def offline_loop(self):
        """Push when the radio has been unreachable for a while (power bank flat,
        unplugged, out of BLE range), and again when it's back."""
        while True:
            await asyncio.sleep(30)
            r = self.radio
            down = time.time() - r.changed_at
            try:
                if not r.connected and not self.offline_alerted and down >= OFFLINE_ALERT_SECS:
                    self.offline_alerted = True
                    if self.db.get_setting("notify_offline", True):
                        mv = (r.latest.get("battery") or {}).get("level")
                        body = (f"No contact for {down / 60:.0f} min — check its power."
                                + (f" Last battery reading {mv / 1000:.2f} V." if mv else ""))
                        log.info("radio offline alert: %s", body)
                        await self.hub.alert("Radio offline", body, "radio-offline", "/#/radio")
                elif r.connected and self.offline_alerted:
                    self.offline_alerted = False
                    if self.db.get_setting("notify_offline", True):
                        await self.hub.alert("Radio back online", "Connected again — messages can be sent.",
                                             "radio-offline", "/#/radio")
            except Exception as e:  # noqa: BLE001 - never let the watcher die
                log.warning("offline watch: %s", e)

    # ---------- repeater battery ----------
    def watched(self) -> dict:
        return self.db.get_setting("battery_watch", {}) or {}

    def readings(self) -> dict:
        return self.db.get_setting("battery_readings", {}) or {}

    async def check(self, pk: str) -> dict:
        """Log in (guest password if set) and ask one repeater for its status."""
        cfg = self.watched().get(pk, {})
        r = self.radio
        c = r.contact(pk)
        reading = {"ts": int(time.time() * 1000)}
        try:
            if cfg.get("password") is not None:
                ev = await r.mesh("send_login_sync", c, cfg.get("password") or "", min_timeout=OTA_TIMEOUT)
                if not ev or ev.type != EventType.LOGIN_SUCCESS:
                    raise ValueError("login failed or timed out")
            st = await r.mesh("req_status_sync", c, min_timeout=OTA_TIMEOUT)
            if not st:
                raise ValueError("no reply (timed out)")
            v = (st.get("bat") or 0) / 1000
            reading.update({"v": round(v, 3), "pct": cell_pct(v) if v else None,
                            "uptime": st.get("uptime"), "noise_floor": st.get("noise_floor"),
                            "tx_queue_len": st.get("tx_queue_len"), "full_evts": st.get("full_evts"),
                            "last_snr": st.get("last_snr")})
        except Exception as e:  # noqa: BLE001
            reading["error"] = str(e)
        prev = self.readings().get(pk, {})
        if "error" in reading and "v" in prev:      # keep the last good value alongside the error
            reading.update({k: prev[k] for k in ("v", "pct") if k in prev}, good_ts=prev.get("good_ts", prev["ts"]))
        elif "v" in reading:
            reading["good_ts"] = reading["ts"]
        reading["alerted"] = prev.get("alerted", 0)
        below = int(cfg.get("below", 30))
        if "error" not in reading and reading.get("pct") is not None and reading["pct"] < below:
            if time.time() - reading["alerted"] > BATTERY_ALERT_EVERY:
                reading["alerted"] = time.time()
                name = c.get("adv_name") or pk[:8]
                await self.hub.alert(f"{name} battery low", f"{reading['pct']}% ({reading['v']:.2f} V) — below your {below}% alert",
                                     f"bat:{pk[:12]}", f"/#/contact/{pk[:12]}")
        all_r = self.readings()
        all_r[pk] = reading
        self.db.set_setting("battery_readings", all_r)
        return reading

    async def battery_loop(self):
        await asyncio.sleep(BATTERY_FIRST_SECS)
        while True:
            for pk in list(self.watched()):
                last = self.readings().get(pk, {}).get("ts", 0) / 1000
                if time.time() - last < BATTERY_SECS - 60:
                    continue
                try:
                    r = await self.check(pk)
                    log.info("battery %s: %s", pk[:8], r)
                except Exception as e:  # noqa: BLE001 - contact gone, radio offline…
                    log.info("battery check %s skipped: %s", pk[:8], e)
            self.flush()
            await asyncio.sleep(300)
