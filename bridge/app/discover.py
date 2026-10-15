"""Hashtag-channel discovery.

A channel packet carries chan_hash = first byte of SHA256(key) plus a 2-byte
HMAC of the ciphertext. Hashtag channels use key = SHA256(name)[:16], so for
channels we hear but can't read we can test likely names offline: a matching
hash *and* a valid MAC on a captured packet means we've found the channel.
Private channels (random keys) stay unknowable, as they should.
"""
import hashlib
import hmac

import os

# Generic starting points. Add your own area's towns/topics with DISCOVER_NAMES
# (space/comma separated) in the bridge env file, or via the "Try a name" box.
PLACES = """uk gb england scotland wales ireland london birmingham manchester leeds liverpool newcastle
bristol sheffield nottingham leicester cardiff edinburgh glasgow belfast oxford cambridge brighton
southampton portsmouth plymouth exeter norwich york hull bath reading kent sussex surrey essex devon
cornwall dorset hampshire yorkshire lancashire midlands southeast southwest northwest northeast
usa us canada australia nz newzealand germany deutschland france nederland netherlands belgium
""".split()
TOPICS = """test testing chat general random public help emergency sos alert alerts weather wx news
local meshcore mesh lora radio ham hamradio offgrid prepper preppers bots bot ping echo repeaters
repeater admin dev debug tech jokes memes music sports football games gaming chess trivia quiz
cars bikes cycling hiking walking sailing boats fishing coffee food pubs beer social meetup events
lost found pets dogs cats space astronomy aurora satellites planes adsb trains traffic
roads police fire ambulance flood power outage
""".split()
EXTRA = os.environ.get("DISCOVER_NAMES", "").replace(",", " ").lower().split()


def candidates() -> list[str]:
    names = set()
    for w in PLACES + TOPICS + [e.lstrip("#") for e in EXTRA]:
        names.add(f"#{w}")
        names.add(f"#{w}mesh")
        names.add(f"#{w}-mesh")
    return sorted(names)


def key_for(name: str) -> bytes:
    return hashlib.sha256(name.encode()).digest()[:16]


def hash_for(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:2]


TABLE: dict[str, list[tuple[str, bytes]]] = {}
for _n in candidates():
    _k = key_for(_n)
    TABLE.setdefault(hash_for(_k), []).append((_n, _k))


def mac_ok(key: bytes, mac_hex: str, ct_hex: str) -> bool:
    return hmac.new(key, bytes.fromhex(ct_hex), hashlib.sha256).digest()[:2].hex() == mac_hex


def identify(chan_hash: str, samples: list[tuple[str, str]], extra: list[str] = ()) -> str | None:
    """Return the hashtag name whose key verifies against captured (mac, ciphertext) samples."""
    options = list(TABLE.get(chan_hash, []))
    for n in extra:
        n = n if n.startswith("#") else f"#{n}"
        k = key_for(n.lower())
        if hash_for(k) == chan_hash:
            options.append((n.lower(), k))
    for name, key in options:
        if samples and all(mac_ok(key, m, c) for m, c in samples[:3]):
            return name
    return None
