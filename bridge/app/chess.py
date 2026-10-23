"""Chess over DMs. MeshCore has no game packet, so a game is ordinary short DMs any
client can read, and each move gets the normal delivery receipt:

  ♟g3k new white        challenge; the sender plays white (or black)
  ♟g3k 12. Nf3 #a41c    a move (12... for black) + checksum of the position after it
  ♟g3k resign | ♟g3k draw? | ♟g3k draw ok
  ♟g3k sync <FEN>       the sender's whole position, when the checksums disagree

The PWA plays the rules (chess.js); the bridge only spots these for push text.
"""
import re

PREFIX = "♟"
RE = re.compile(r"^♟[\uFE0E\uFE0F]?([0-9a-z]{3}) (?:new (white|black)|(\d{1,3})(\.\.\.|\.) (\S{2,8}) #([0-9a-f]{4})"
                r"|(resign|draw\?|draw ok)|sync (\S+ [wb] \S+ \S+ \d+ \d+))$")


def parse(text: str) -> dict | None:
    m = RE.match((text or "").strip())
    if not m:
        return None
    game, colour, num, dots, san, check, act, fen = m.groups()
    if colour:
        return {"game": game, "kind": "new", "colour": colour}
    if san:
        return {"game": game, "kind": "move", "ply": (int(num) - 1) * 2 + (dots == "..."),
                "san": san, "check": check, "label": f"{num}{dots} {san}"}
    if act:
        return {"game": game, "kind": {"resign": "resign", "draw?": "draw_offer", "draw ok": "draw_accept"}[act]}
    return {"game": game, "kind": "sync", "fen": fen}


def push_text(text: str) -> str | None:
    """What a push notification says instead of the raw move text."""
    p = parse(text)
    if not p:
        return None
    return {"new": lambda: f"challenged you to chess (you're {'black' if p.get('colour') == 'white' else 'white'})",
            "move": lambda: f"played {p.get('label')}",
            "resign": lambda: "resigned the chess game",
            "draw_offer": lambda: "offers a draw",
            "draw_accept": lambda: "accepted the draw",
            "sync": lambda: "sent their chess position"}[p["kind"]]()
