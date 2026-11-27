import getpass
import os
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from functools import lru_cache

from downkeep.model import ActionError, Move, Stay
from downkeep.origin import host_of
from downkeep.scan import FileRecord, Snapshot

RULES_KEY = "rules"
MAX_RULES, MAX_LIST, MAX_TEXT, MAX_DAYS = 100, 50, 60, 365

KINDS = {
    "documents": "Documents",
    "spreadsheets": "Spreadsheets",
    "presentations": "Presentations",
    "images": "Pictures",
    "videos": "Videos",
    "music": "Music and sound",
    "archives": "Archives",
    "installers": "Installers",
    "books": "Books",
    "fonts": "Fonts",
    "torrents": "Torrents",
    "code": "Code and data",
}
# Left out on purpose: .html (a saved page breaks without its folder), .ts, .key, .pub, .bin, .sub (more than one
# meaning), keys and passwords (.pem, .p12, .kdbx, .ovpn)
EXTENSIONS = {
    kind: frozenset(text.split())
    for kind, text in {
        "documents": "pdf doc docx docm dot dotx odt ott rtf txt md tex pages wpd wps xps oxps",
        "spreadsheets": "xls xlsx xlsm xlsb xlt xltx ods ots csv tsv numbers",
        "presentations": "ppt pptx pptm pps ppsx pot potx odp otp",
        "images": "jpg jpeg jfif png gif bmp webp svg ico tif tiff heic heif avif jxl psd ai eps xcf kra "
        "raw cr2 cr3 nef arw dng orf rw2",
        "videos": "mp4 m4v mkv mov avi wmv flv webm mpg mpeg 3gp ogv vob mts m2ts srt vtt ass",
        "music": "mp3 m4a aac flac wav ogg oga opus wma aif aiff mid midi amr m4b",
        "archives": "zip 7z rar tar gz tgz bz2 tbz2 xz txz zst lz lzma lz4 cab",
        "installers": "exe msi msix msixbundle appx appxbundle appinstaller msu deb rpm appimage flatpak flatpakref "
        "snap run pkg dmg iso img apk xapk",
        "books": "epub mobi azw azw3 fb2 cbz cbr djvu",
        "fonts": "ttf otf ttc woff woff2",
        "torrents": "torrent",
        "code": "py ipynb js jsx tsx css c h cpp hpp cs java kt go rs rb php pl lua r swift sh bash bat cmd ps1 "
        "json jsonl xml yaml yml toml sql sqlite parquet log diff patch",
    }.items()
}
_KIND = {extension: kind for kind, extensions in EXTENSIONS.items() for extension in extensions}
_SPLIT_ARCHIVE = re.compile(r"\.(?:[rz]\d\d|(?:7z|zip|rar|tar|gz)\.\d{3})$")
_RUNS = EXTENSIONS["installers"] - {"iso", "img"} | frozenset(
    "bat cmd com scr pif hta js jse vbs vbe wsf ps1 sh lnk jar reg cpl".split()
)
_LOOKS_HARMLESS = ("documents", "spreadsheets", "presentations", "images", "videos", "music", "archives", "books")

_RESERVED = re.compile(r"(?:con|prn|aux|nul|clock\$|conin\$|conout\$|(?:com|lpt)[0-9¹²³])\s*(?:\..*)?", re.I | re.S)
_EXTENSION = re.compile(r"[a-z0-9][a-z0-9+_-]*(?:\.[a-z0-9+_-]+)*")
_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
_GAPS = re.compile(r"[\s_.\-]+")
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])")


def _default(name, folder, *kinds):
    return {"id": name, "folder": folder, "kinds": list(kinds), "extensions": [], "words": [], "sites": [],
            "on": True, "auto": True, "days": 1}


DEFAULT_RULES: list[dict] = [
    _default("documents", "Documents", "documents", "spreadsheets", "presentations"),
    _default("images", "Images", "images"),
    _default("videos", "Videos", "videos"),
    _default("music", "Music", "music"),
    _default("archives", "Archives", "archives"),
    _default("installers", "Installers", "installers"),
    _default("books", "Books", "books"),
    _default("fonts", "Fonts", "fonts"),
    _default("torrents", "Torrents", "torrents"),
    _default("code", "Code", "code"),
]
_DEFAULT_IDS = [rule["id"] for rule in DEFAULT_RULES]


def kind_of(name: str) -> str | None:
    name = name.lower()
    if _SPLIT_ARCHIVE.search(name):
        return "archives"
    return _KIND.get(os.path.splitext(name)[1][1:])


# Matching


def match(file: FileRecord, rules: Sequence[dict], host: str | None = None) -> tuple[dict, str] | None:
    """The first rule that fits, and why. Rules must have been through check_rules."""
    pretends = _pretends(file.name)
    for rule in rules:
        if rule["on"]:
            why = _why(file.name, rule, host, pretends)
            if why:
                return rule, why
    return None


def _why(name, rule, host, pretends):
    if not pretends:  # A disguised program is filed as a program, never with the invoices it imitates
        words = _texts(name)
        for word in rule["words"]:
            if any(_has_word(text, _plain(word)) for text in words):
                return f"The name contains {word}"
        if host and any(host == site or host.endswith("." + site) for site in rule["sites"]):
            return f"Came from {host}"
    warning = f"Named like a .{pretends[0]} file but really .{pretends[1]}. " if pretends else ""
    for extension in rule["extensions"]:
        if name.lower().endswith("." + extension):
            return f"{warning}.{extension} files go to {rule['folder']}"
    kind = kind_of(name)
    if kind in rule["kinds"]:
        return f"{warning}{KINDS[kind]} go to {rule['folder']}"
    return None


def _pretends(name):
    stem, last = os.path.splitext(name.lower())
    inner = os.path.splitext(stem.rstrip())[1]
    if last[1:] in _RUNS and kind_of("x" + inner) in _LOOKS_HARMLESS:
        return inner[1:], last[1:]
    return None


def _texts(name):
    stem = os.path.splitext(name)[0]
    return {_plain(stem), _plain(_CAMEL.sub(" ", stem))}


@lru_cache(4096)
def _plain(text):
    return _GAPS.sub(" ", unicodedata.normalize("NFKC", text).casefold()).strip()


def _has_word(text, word):
    start = text.find(word)
    while start >= 0:
        end = start + len(word)
        if _apart(text[:start][-1:], word[0]) and _apart(word[-1], text[end : end + 1]):
            return True
        start = text.find(word, start + 1)
    return False


def _apart(left, right):
    """invoice2 and cs101a are two words each. Scripts without upper and lower case have no such rule."""
    return not (left.isdigit() and right.isdigit() or left.islower() and right.islower())


# Planning


def look_in(folder: str) -> dict[str, bool]:
    """Every name in the folder, and whether it is a real folder rather than a file or a link."""
    with os.scandir(folder) as listing:
        return {
            entry.name: entry.is_dir(follow_symlinks=False) and not entry.is_symlink() and not entry.is_junction()
            for entry in listing
        }


def plan(
    snapshot: Snapshot,
    rules: Sequence[dict],
    hosts: Mapping[str, str] | None = None,
    look: Callable[[str], Mapping[str, bool]] = look_in,
) -> tuple[list[Move], list[Stay]]:
    """What would move where. Reads names only, through look; changes nothing."""
    moves, stays = [], []
    places = _Places(snapshot.folder, look)
    rules = [rule | {"folder": places.spelled(rule["folder"])} for rule in rules]
    for file in sorted(snapshot.files, key=lambda file: file.name):
        found = match(file, rules, (hosts or {}).get(file.path))
        if not found:
            stays.append(Stay(file.name, "No rule matches"))
        elif file.links != 1:
            stays.append(Stay(file.name, "The same file also exists under another name"))
        else:
            rule, why = found
            problem = places.claim(rule["folder"], file.name)
            if problem:
                stays.append(Stay(file.name, problem))
            else:
                moves.append(Move(file, rule["folder"], why))
    return moves, stays


class _Places:
    def __init__(self, root, look):
        self.root, self.look, self.taken = root, look, {}
        try:
            self.top = dict(look(root))
        except OSError:
            self.top = None

    def spelled(self, wanted):
        """An existing images folder is used instead of making Images next to it."""
        alike = sorted(entry for entry in self.top or () if same(entry) == same(wanted))
        return alike[0] if alike and wanted not in alike else wanted

    def claim(self, folder, name):
        """Reserves the name in the folder, or returns why it can't go there."""
        if self.top is None:
            return "Couldn't check the folder"
        if not self.top.get(folder, True):
            return f"Something else here is already named {folder}"
        if folder not in self.taken:
            self.taken[folder] = self._names(folder) if folder in self.top else {}
        names = self.taken[folder]
        if names is None:
            return f"Couldn't check what is in {folder}"
        if same(name) in names:
            return names[same(name)]
        names[same(name)] = f"Another file with the same name is also going to {folder}"
        return None

    def _names(self, folder):
        try:
            found = self.look(os.path.join(self.root, folder))
        except OSError:
            return None
        return {same(entry): f"{folder} already has a file with this name" for entry in found}


def same(name: str) -> str:
    """Equal for names that some system would treat as the same name."""
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", name).casefold())


# Checking


def saved_rules(store) -> list[dict]:
    return check_rules(store.get(RULES_KEY, DEFAULT_RULES))


def check_rules(value: object) -> list[dict]:
    """A clean copy: own rules first in their order, then the defaults. A default that is missing comes back off."""
    if not isinstance(value, list):
        raise ActionError("The rules are not a list")
    if len(value) > MAX_RULES:
        raise ActionError(f"There can be at most {MAX_RULES} rules")
    rules = [_check_rule(item) for item in value]
    used = [rule["id"] for rule in rules if rule["id"]]
    if len(used) != len(set(used)):
        raise ActionError("Two rules have the same id")
    number = 0
    for rule in rules:
        while not rule["id"]:
            number += 1
            if f"own-{number}" not in used:
                rule["id"] = f"own-{number}"
    given = {rule["id"]: rule for rule in rules}
    own = [rule for rule in rules if rule["id"] not in _DEFAULT_IDS]
    rules = own + [given.get(rule["id"]) or _copy(rule) | {"on": False} for rule in DEFAULT_RULES]
    if len(rules) > MAX_RULES:
        raise ActionError(f"There can be at most {MAX_RULES} rules")
    spelling = {}
    for rule in rules:
        rule["folder"] = spelling.setdefault(same(rule["folder"]), rule["folder"])
    return rules


def _copy(rule):
    return {key: list(value) if isinstance(value, list) else value for key, value in rule.items()}


def _check_rule(item):
    blank = _default("", "")
    if not isinstance(item, dict) or not all(isinstance(key, str) for key in item):
        raise ActionError("A rule is not in the right form")
    if set(item) - set(blank):
        raise ActionError("A rule has a setting Downkeep doesn't know")
    rule = blank | item
    folder = _check_folder(rule["folder"])
    if not isinstance(rule["id"], str) or rule["id"] and not _ID.fullmatch(rule["id"]):
        raise ActionError(f"The rule for {folder} has an id that can't be used")
    for key in ("on", "auto"):
        if not isinstance(rule[key], bool):
            raise ActionError(f"The rule for {folder} must be either on or off")
    if type(rule["days"]) is not int or not 0 <= rule["days"] <= MAX_DAYS:
        raise ActionError(f"The days for {folder} must be a whole number from 0 to {MAX_DAYS}")
    for key, check in (("kinds", _check_kind), ("extensions", _check_extension),
                       ("words", _check_word), ("sites", _check_site)):
        rule[key] = _check_list(rule[key], check, folder)
    if not any(rule[key] for key in ("kinds", "extensions", "words", "sites")):
        raise ActionError(f"The rule for {folder} doesn't say which files go there")
    return rule | {"folder": folder}


def _check_list(values, check, folder):
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ActionError(f"The rule for {folder} is not in the right form")
    if len(values) > MAX_LIST:
        raise ActionError(f"The rule for {folder} can list at most {MAX_LIST} of each")
    if any(len(value) > 4 * MAX_TEXT for value in values):
        raise ActionError(f"The rule for {folder} has a text that is too long")
    return list(dict.fromkeys(check(value) for value in values))


def _check_folder(value):
    if not isinstance(value, str) or len(value) > 4 * MAX_TEXT:
        raise ActionError(f"A folder name can have at most {MAX_TEXT} characters")
    name = unicodedata.normalize("NFC", value)
    if not name.strip():
        raise ActionError("A folder needs a name")
    if len(name) > MAX_TEXT:
        raise ActionError(f"A folder name can have at most {MAX_TEXT} characters")
    if not name.isprintable() or any(char in '<>:"/\\|?*' for char in name):
        raise ActionError('A folder name can\'t contain < > : " / \\ | ? * or invisible characters')
    if name.startswith((".", "~", " ")):
        raise ActionError("A folder name can't start with a dot, a space or ~")
    if name.endswith((".", " ")):
        raise ActionError("A folder name can't end with a dot or a space")
    if _RESERVED.fullmatch(name):
        raise ActionError(f"{name} is a name Windows keeps for itself")
    return name


def _check_kind(value):
    if value not in KINDS:
        raise ActionError("A rule uses a kind of file Downkeep doesn't know")
    return value


def _check_extension(value):
    extension = value.strip().lower().removeprefix("*").removeprefix(".")
    if len(extension) > 20 or not _EXTENSION.fullmatch(extension):
        raise ActionError("A file ending looks like pdf or tar.gz")
    return extension


def _check_word(value):
    word = unicodedata.normalize("NFC", value).strip()
    if not word.isprintable() or len(word) > MAX_TEXT or not _plain(word):
        raise ActionError(f"A word needs visible letters or numbers, at most {MAX_TEXT}")
    return word


def _check_site(value):
    text = value.strip()
    site = host_of(text if "://" in text else "https://" + text)
    site = site.removeprefix("www.") if site else ""
    if "." not in site.strip(".") or site.startswith("."):
        raise ActionError("A site looks like example.com")
    return site


# Suggestions

MOST_SUGGESTED = 3
FEWEST_FILES = 3
SHORTEST_WORD = 4
MOSTLY = 0.8
PLAIN_HOSTS = frozenset("""
    www www2 m mobile mail web cdn dl download downloads files static media assets lms moodle portal canvas classroom
    blackboard learn elearning my app apps account accounts login secure docs example
""".split())
TEST_ENDINGS = ("test", "example", "invalid", "localhost", "local")
BRANDS = {"github.com": "GitHub", "githubusercontent.com": "GitHub", "whatsapp.com": "WhatsApp",
          "whatsapp.net": "WhatsApp", "youtube.com": "YouTube", "linkedin.com": "LinkedIn",
          "drive.google.com": "Google Drive", "drive.usercontent.google.com": "Google Drive",
          "chatgpt.com": "ChatGPT", "mail.google.com": "Gmail", "mail-attachment.googleusercontent.com": "Gmail"}
# Only words that read well with an s; anything else keeps the person's word
COUNTABLE = frozenset("""
    invoice receipt bill lecture statement ticket assignment report payslip certificate resume letter form note slide
    screenshot
""".split())
# Screenshot is not here on purpose: a Screenshots folder is one people really want
STOP_WORDS = frozenset("""
    copy final new file files document documents download downloads image images img pic pics photo photos picture
    video videos vid audio music song untitled scan scanned scans draft edit edited export output version ver rev
    test temp tmp backup old latest beta alpha release full setup install installer portable win windows linux mac
    macos amd arm the and for with from your our you this that are was not all page pages part dsc pxl wa
    january february march april may june july august september october november december
    jan feb mar apr jun jul aug sep sept oct nov dec monday tuesday wednesday thursday friday saturday sunday
    preview previews removebg nobg compressed compress resized resize cropped crop converted optimized optimised
    exported thumb thumbnail thumbnails watermark watermarked merged rotated enhanced upscaled original orig
    modified updated revised fixed copia kopie lowres hires small large medium unnamed noname
""".split())
WEB_WORDS = frozenset("com org net edu gov int mil info biz io co in uk us ac www http https ftp mailto html htm php".split())
_LABEL = r"(?:[^\W_]|-)+"
_WEB = re.compile(rf"""
    (?:https?|ftp)://\S* | mailto:\S*
  | (?:[^\W_]|[.+])+@{_LABEL}(?:\.{_LABEL})+
  | (?:{_LABEL}\.)+(?:com|net|org|edu|gov|io|info|biz|(?:co|ac|com|org|net|edu|gov)\.[a-z]{{2}})(?![^\W_])
  | www\d?(?:\.{_LABEL})+
""", re.I | re.X)
_ID_PART = re.compile(r"(?:^|-)\d{5,}(?:-|$)")
_TOKEN = re.compile(r"[^\W\d_]+")


def suggest_name(rule: dict) -> str:
    """A plain folder name for what the rule matches. Always one check_rules accepts."""
    names = [*map(_site_name, rule.get("sites") or ()), *map(_word_name, rule.get("words") or ()),
             *(KINDS.get(kind) for kind in rule.get("kinds") or ()),
             *(f"{extension.upper()} files" for extension in rule.get("extensions") or ())]
    for name in names:
        try:
            return _check_folder(name)
        except ActionError:
            continue
    return "New folder"


def _site_name(site):
    if not isinstance(site, str) or ":" in site:
        return None
    site = site.lower().strip(".")
    brand = next((name for host, name in BRANDS.items() if site == host or site.endswith("." + host)), None)
    labels = [label for label in site.split(".") if label]
    if brand or len(labels) < 2 or labels[-1] in TEST_ENDINGS or all(label.isdigit() for label in labels):
        return brand
    # The first useful label is the name people know: bbc in www.bbc.co.uk, college in lms.college.example.edu
    useful = [label for label in labels[:-1] if label not in PLAIN_HOSTS]
    if not useful or _ID_PART.search(useful[0]):
        return None
    try:
        main = useful[0].encode("ascii").decode("idna")
    except UnicodeError:
        main = useful[0]
    return main[:1].upper() + main[1:]


def _word_name(word):
    if not isinstance(word, str) or not word.strip():
        return None
    first = word.split()[0]
    named = first[:1].upper() + first[1:]
    return named + "s" if first.lower() in COUNTABLE else named


def suggest(
    snapshot: Snapshot,
    rules: Sequence[dict],
    hosts: Mapping[str, str] | None = None,
    look: Callable[[str], Mapping[str, bool]] = look_in,
    own_names: Iterable[str] | None = None,
) -> list[dict]:
    """Up to three new folders worth having here, the ones covering the most files first. Reads names only.
    own_names are the person's own names, never suggested; by default they come from the account."""
    hosts = hosts or {}
    files = snapshot.files
    person = _name_words(_account_names() if own_names is None else own_names)
    try:
        taken = {same(name) for name in look(snapshot.folder)}
    except OSError:
        taken = set()
    taken |= {same(rule["folder"]) for rule in rules}
    ids = {rule["id"] for rule in rules}
    chosen = []
    for rule, why in _candidates(files, hosts, person):
        covered = frozenset(
            file.name for file in files if _why(file.name, rule, hosts.get(file.path), _pretends(file.name))
        )
        own = {_own_folder(file, rules, hosts) for file in files if file.name in covered}
        if len(covered) < FEWEST_FILES or (len(own) == 1 and None not in own):
            continue
        splits = bool(rule["words"]) and _only_splits_a_type(
            rule["words"][0], [file for file in files if file.name in covered], rules, hosts)
        rule = rule | {"folder": suggest_name(rule)}
        chosen.append((len(covered), rule, why, covered, splits))
    chosen.sort(key=lambda item: (-item[0], same(item[1]["folder"]), item[2]))
    found, claimed, split = [], [], []
    for count, rule, why, covered, splits in chosen:
        # Mostly the files of a bigger suggestion, or for words, of a bigger word that only split a type: chill in lofi
        others = claimed + split if rule["words"] else claimed
        if any(len(covered & other) >= MOSTLY * count for other in others):
            continue
        if splits:
            split.append(covered)
            continue
        if same(rule["folder"]) in taken:
            continue
        taken.add(same(rule["folder"]))
        try:
            rule = _check_rule(rule | {"id": _free_id(ids)})
        except ActionError:
            continue
        ids.add(rule["id"])
        claimed.append(covered)
        found.append({"folder": rule["folder"], "why": why.format(count=count), "rule": rule, "files": count})
        if len(found) == MOST_SUGGESTED:
            break
    return found


def _candidates(files, hosts, person=frozenset()):
    """Sites and words seen in at least three files, most common first, as rules and reasons."""
    sites, words = {}, {}
    for file in files:
        host = hosts.get(file.path)
        if host and _site_name(host):
            sites.setdefault(host.removeprefix("www."), set()).add(file.name)
        for word in _words(file.name, person):
            words.setdefault(word, set()).add(file.name)
    found = [(len(names), "site", site) for site, names in sites.items()]
    found += [(len(names), "word", word) for word, names in words.items()]
    found = sorted((item for item in found if item[0] >= FEWEST_FILES), key=lambda item: (-item[0], item[1], item[2]))
    for _, kind, value in found[:30]:
        blank = _default("", "") | {"kinds": []}
        if kind == "site":
            yield blank | {"sites": [value]}, f"{{count}} files came from {value}"
        else:
            yield blank | {"words": [value]}, f"{{count}} files have {value} in the name"


def _words(name, person=frozenset()):
    tokens = set()
    for text in _texts(_WEB.sub(" ", name)):
        for token in _TOKEN.findall(text):
            if (len(token) >= SHORTEST_WORD and token not in STOP_WORDS and token not in WEB_WORDS
                    and token not in _KIND and token not in person and _not_private(token)):
                tokens.add(token)
    return tokens


def _only_splits_a_type(word, files, rules, hosts):
    """A word only splits up a built-in folder when nearly all its files go to that same one anyway.
    Documents is left out: papers are what people sort by topic, and it holds slides and sheets too."""
    if word in COUNTABLE:
        return False
    built_in = Counter()
    for file in files:
        found = match(file, rules, hosts.get(file.path))
        if found and found[0]["id"] in _DEFAULT_IDS and found[0]["id"] != "documents":
            built_in[found[0]["id"]] += 1
    return bool(built_in) and built_in.most_common(1)[0][1] >= MOSTLY * len(files)


def _account_names():
    names = []
    if os.name == "nt":
        names += [os.environ.get("USERNAME", ""), _windows_display_name()]
    else:
        try:
            import pwd

            entry = pwd.getpwuid(os.getuid())
            names += [entry.pw_name, entry.pw_gecos.split(",")[0]]
        except (ImportError, KeyError, OSError, AttributeError):
            pass
    try:
        names.append(getpass.getuser())
    except (ImportError, KeyError, OSError):
        pass
    return names


def _windows_display_name():
    try:
        import ctypes

        size = ctypes.c_ulong(0)
        get = ctypes.windll.secur32.GetUserNameExW
        get(3, None, ctypes.byref(size))
        buffer = ctypes.create_unicode_buffer(size.value + 1)
        return buffer.value if get(3, buffer, ctypes.byref(size)) else ""
    except (ImportError, AttributeError, OSError, ValueError):
        return ""


def _name_words(names):
    """Each part of each name, and the parts joined, as _words would see them."""
    found = set()
    for name in names:
        if isinstance(name, str):
            parts = _TOKEN.findall(_plain(_CAMEL.sub(" ", name)))
            found.update(parts, ["".join(parts)] if parts else [])
    return frozenset(found)


def _not_private(token):
    """Random looking strings, such as ids and codes, are never shown back as a suggestion."""
    letters = set(token)
    hexish = len(token) >= 6 and letters <= set("abcdef")
    vowels = any(char in "aeiouy" for char in token) or not token.isascii()
    return len(token) <= 30 and not hexish and vowels


def _own_folder(file, rules, hosts):
    found = match(file, rules, hosts.get(file.path))
    return same(found[0]["folder"]) if found and found[0]["id"] not in _DEFAULT_IDS else None


def _free_id(ids):
    number = 1
    while f"own-{number}" in ids:
        number += 1
    return f"own-{number}"
