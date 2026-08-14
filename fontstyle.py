import json
import os
import string

STATE_FILE = "font_state.json"


def _range_map(upper_base=None, lower_base=None, digit_base=None, exceptions=None):
    """Builds a char->char mapping from a Unicode Mathematical Alphanumeric
    block. `exceptions` overrides/extends it for the handful of codepoints
    Unicode reserves (e.g. italic 'h', circled digits)."""
    mapping = {}
    if upper_base is not None:
        for i, ch in enumerate(string.ascii_uppercase):
            mapping[ch] = chr(upper_base + i)
    if lower_base is not None:
        for i, ch in enumerate(string.ascii_lowercase):
            mapping[ch] = chr(lower_base + i)
    if digit_base is not None:
        for i, ch in enumerate(string.digits):
            mapping[ch] = chr(digit_base + i)
    if exceptions:
        mapping.update(exceptions)
    return mapping


_circled_digits = {str(n): chr(0x2460 + n - 1) for n in range(1, 10)}
_circled_digits["0"] = chr(0x24EA)

_small_caps_map = {
    "a": "ᴀ", "b": "ʙ", "c": "ᴄ", "d": "ᴅ", "e": "ᴇ", "f": "ꜰ", "g": "ɢ",
    "h": "ʜ", "i": "ɪ", "j": "ᴊ", "k": "ᴋ", "l": "ʟ", "m": "ᴍ", "n": "ɴ",
    "o": "ᴏ", "p": "ᴘ", "q": "ǫ", "r": "ʀ", "t": "ᴛ", "u": "ᴜ", "v": "ᴠ",
    "w": "ᴡ", "y": "ʏ", "z": "ᴢ",
}
_small_caps_map.update({k.upper(): v for k, v in _small_caps_map.items()})

# key -> (display name shown in the panel, char map)
FONTS = {
    "bold": ("𝗕𝗼𝗹𝗱", _range_map(0x1D400, 0x1D41A, 0x1D7CE)),
    "italic": ("𝘐𝘵𝘢𝘭𝘪𝘤", _range_map(0x1D434, 0x1D44E, None, {"h": "\u210E"})),
    "bold_italic": ("𝘽𝙤𝙡𝙙 𝙄𝙩𝙖𝙡𝙞𝙘", _range_map(0x1D468, 0x1D482, None)),
    "double_struck": ("𝔻𝕠𝕦𝕓𝕝𝕖 𝕊𝕥𝕣𝕦𝕔𝕜", _range_map(0x1D538, 0x1D552, 0x1D7D8, {
        "C": "\u2102", "H": "\u210D", "N": "\u2115", "P": "\u2119",
        "Q": "\u211A", "R": "\u211D", "Z": "\u2124",
    })),
    "monospace": ("𝙼𝚘𝚗𝚘𝚜𝚙𝚊𝚌𝚎", _range_map(0x1D670, 0x1D68A, 0x1D7F6)),
    "fraktur": ("𝔉𝔯𝔞𝔨𝔱𝔲𝔯", _range_map(0x1D504, 0x1D51E, None, {
        "C": "\u212D", "H": "\u210C", "I": "\u2111", "R": "\u211C", "Z": "\u2128",
    })),
    "circled": ("Ⓒⓘⓡⓒⓛⓔⓓ", _range_map(0x24B6, 0x24D0, None, _circled_digits)),
    "fullwidth": ("Ｆｕｌｌｗｉｄｔｈ", _range_map(0xFF21, 0xFF41, 0xFF10)),
    "sans": ("𝖲𝖺𝗇𝗌", _range_map(0x1D5A0, 0x1D5BA, 0x1D7E2)),
    "sans_bold": ("𝗦𝗮𝗻𝘀 𝗕𝗼𝗹𝗱", _range_map(0x1D5D4, 0x1D5EE, 0x1D7EC)),
    "script": ("𝒮𝒸𝓇𝒾𝓅𝓉", _range_map(0x1D49C, 0x1D4B6, None, {
        "B": "\u212C", "E": "\u2130", "F": "\u2131", "H": "\u210B", "I": "\u2110",
        "L": "\u2112", "M": "\u2133", "R": "\u211B",
        "e": "\u212F", "g": "\u210A", "o": "\u2134",
    })),
    "small_caps": ("Sᴍᴀʟʟ Cᴀᴘs", dict(_small_caps_map)),
}

_state = {"active": None}


def _save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(_state, f)
    except Exception:
        pass


def _load_state():
    global _state
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            _state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        _state = {"active": None}


_load_state()


def set_font(key):
    _state["active"] = key
    _save_state()


def get_active():
    return _state.get("active")


def apply(text: str) -> str:
    key = _state.get("active")
    if not key or key not in FONTS:
        return text
    _, mapping = FONTS[key]
    return "".join(mapping.get(ch, ch) for ch in text)
