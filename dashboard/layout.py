"""Pure layout engine: validate a draft, diff it against the live server, plan positions.

No Discord or network imports, so every rule here is unit-tested (tests/test_layout.py).

Shapes
------
snapshot (what the server looks like now), all ids are STRINGS (JS can't hold 64-bit ints):
    {"uncategorized": [chan, ...], "categories": [{"id", "name", "channels": [chan, ...]}, ...]}
    chan = {"id", "name", "type"}

draft (what the dashboard wants it to look like):
    {"uncategorized": [chan, ...],
     "categories": [{"id": str|None, "name", "why", "channels": [chan, ...]}, ...],
     "deleted": [{"id": ...}, ...]}
    chan = {"id": str|None, "name", "type", "topic", "why"}      id None = brand-new channel

Safety rules enforced here (never trusted from the client or from the AI):
  * an existing item keeps its real type; only text/voice can be created
  * existing channels missing from a draft are KEPT, never silently deleted
  * deletion happens only for ids listed in draft["deleted"]
  * Discord limits: 500 channels+categories per server, 50 channels per category
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata

NAME_MAX = 100
TOPIC_MAX = 1024
WHY_MAX = 200
MAX_TOTAL = 500
MAX_PER_CATEGORY = 50

TEXT_NAMED = {"text", "announcement", "forum", "media"}  # names are lowercase-with-hyphens
VOICE_GROUP = {"voice", "stage"}  # Discord always lists these below text channels
CREATABLE = {"text", "voice"}


class LayoutError(ValueError):
    pass


def group_of(ctype: str) -> int:
    return 1 if ctype in VOICE_GROUP else 0


def clean_name(name, kind: str) -> str:
    """Normalise a name the way Discord will (text channels: lowercase, hyphens)."""
    s = unicodedata.normalize("NFC", name if isinstance(name, str) else "")
    s = re.sub(r"\s+", " ", s)
    s = "".join(ch for ch in s if ch.isprintable() or ch == "‍")  # keep emoji joiners
    s = s.strip()
    if kind in TEXT_NAMED:
        s = re.sub(r"-{2,}", "-", s.lower().replace(" ", "-")).strip("-")
    return s[:NAME_MAX].strip()


def _short(value, limit: int):
    if isinstance(value, str) and value.strip():
        return value.strip()[:limit]
    return None


def index_snapshot(snap: dict):
    cats, chans = {}, {}
    for cat in snap.get("categories", []):
        cats[cat["id"]] = cat
        for ch in cat["channels"]:
            chans[ch["id"]] = (ch, cat["id"])
    for ch in snap.get("uncategorized", []):
        chans[ch["id"]] = (ch, None)
    return cats, chans


def snapshot_to_draft(snap: dict) -> dict:
    """The starting draft is simply the live layout."""
    def conv(ch):
        return {"id": ch["id"], "name": ch["name"], "type": ch["type"], "topic": None, "why": None}

    return {
        "uncategorized": [conv(c) for c in snap["uncategorized"]],
        "categories": [
            {"id": c["id"], "name": c["name"], "why": None, "channels": [conv(x) for x in c["channels"]]}
            for c in snap["categories"]
        ],
        "deleted": [],
    }


def validate_draft(draft, snap: dict) -> dict:
    """Return {"draft": cleaned, "errors": [...], "warnings": [...]}."""
    if not isinstance(draft, dict):
        raise LayoutError("The draft must be a JSON object.")
    cats_idx, chans_idx = index_snapshot(snap)
    errors, warnings = [], []
    seen = set()
    counter = [0]

    def new_tmp() -> str:
        counter[0] += 1
        return f"new:{counter[0]}"

    def ident(raw):
        rid = raw.get("id")
        return str(rid) if rid not in (None, "") else None

    def clean_channel(raw, where):
        if not isinstance(raw, dict):
            warnings.append(f"Ignored an invalid entry in {where}.")
            return None
        rid = ident(raw)
        if rid in chans_idx:
            if rid in seen:
                warnings.append(f'"{raw.get("name")}" appeared twice; the duplicate was ignored.')
                return None
            seen.add(rid)
            orig = chans_idx[rid][0]
            ctype = orig["type"]
            name = clean_name(raw.get("name"), ctype)
            if not name:
                warnings.append(f'Empty name for "{orig["name"]}"; kept the original.')
                name = orig["name"]
            elif name == clean_name(orig["name"], ctype):
                name = orig["name"]  # no spurious rename diff
            return {"id": rid, "tmp": None, "name": name, "type": ctype, "topic": None, "why": _short(raw.get("why"), WHY_MAX)}
        ctype = raw.get("type") if raw.get("type") in CREATABLE else "text"
        name = clean_name(raw.get("name"), ctype)
        if not name:
            errors.append(f"A new channel in {where} has no name.")
            name = "unnamed"
        topic = _short(raw.get("topic"), TOPIC_MAX) if ctype == "text" else None
        return {"id": None, "tmp": new_tmp(), "name": name, "type": ctype, "topic": topic, "why": _short(raw.get("why"), WHY_MAX)}

    clean_cats, clean_unc = [], []
    by_cat = {}

    for raw in draft.get("categories") or []:
        if not isinstance(raw, dict):
            continue
        cid = ident(raw)
        existing = cid in cats_idx
        if existing and cid in seen:
            warnings.append(f'Category "{raw.get("name")}" appeared twice; the duplicate was ignored.')
            continue
        if existing:
            seen.add(cid)
            orig = cats_idx[cid]
            name = clean_name(raw.get("name"), "category")
            if not name:
                warnings.append(f'Empty name for category "{orig["name"]}"; kept the original.')
                name = orig["name"]
            entry = {"id": cid, "tmp": None, "name": name, "why": _short(raw.get("why"), WHY_MAX), "channels": []}
        else:
            name = clean_name(raw.get("name"), "category")
            if not name:
                errors.append("A new category has no name.")
                name = "unnamed"
            entry = {"id": None, "tmp": new_tmp(), "name": name, "why": _short(raw.get("why"), WHY_MAX), "channels": []}
        for rch in raw.get("channels") or []:
            ch = clean_channel(rch, f'category "{name}"')
            if ch:
                entry["channels"].append(ch)
        clean_cats.append(entry)
        if entry["id"]:
            by_cat[entry["id"]] = entry

    for rch in draft.get("uncategorized") or []:
        ch = clean_channel(rch, "the top level")
        if ch:
            clean_unc.append(ch)

    deleted_ids = set()
    for d in draft.get("deleted") or []:
        if isinstance(d, dict):
            did = ident(d)
            if did and (did in cats_idx or did in chans_idx):
                deleted_ids.add(did)
    deleted_ids -= seen  # an item that is still placed in the draft is not deleted

    # Anything the draft forgot is kept where it was (never silently deleted)
    for cid, orig in cats_idx.items():
        if cid not in seen and cid not in deleted_ids:
            warnings.append(f'Category "{orig["name"]}" was missing from the draft, so it was kept.')
            seen.add(cid)
            entry = {"id": cid, "tmp": None, "name": orig["name"], "why": None, "channels": []}
            clean_cats.append(entry)
            by_cat[cid] = entry
    for chid, (orig, parent) in chans_idx.items():
        if chid in seen or chid in deleted_ids:
            continue
        warnings.append(f'Channel "{orig["name"]}" was missing from the draft, so it was kept.')
        seen.add(chid)
        item = {"id": chid, "tmp": None, "name": orig["name"], "type": orig["type"], "topic": None, "why": None}
        (by_cat[parent]["channels"] if parent in by_cat else clean_unc).append(item)

    # Discord shows text-like channels above voice-like ones inside every container
    clean_unc.sort(key=lambda c: group_of(c["type"]))
    for c in clean_cats:
        c["channels"].sort(key=lambda x: group_of(x["type"]))

    total = len(clean_cats) + len(clean_unc) + sum(len(c["channels"]) for c in clean_cats)
    if total > MAX_TOTAL:
        errors.append(f"A server can have at most {MAX_TOTAL} channels and categories (this draft has {total}).")
    for c in clean_cats:
        if len(c["channels"]) > MAX_PER_CATEGORY:
            errors.append(f'Category "{c["name"]}" has {len(c["channels"])} channels; the limit is {MAX_PER_CATEGORY}.')

    deleted = []
    for did in sorted(deleted_ids, key=lambda x: int(x) if x.isdigit() else 0):
        if did in cats_idx:
            deleted.append({"id": did, "name": cats_idx[did]["name"], "type": "category"})
        else:
            orig = chans_idx[did][0]
            deleted.append({"id": did, "name": orig["name"], "type": orig["type"]})

    return {
        "draft": {"uncategorized": clean_unc, "categories": clean_cats, "deleted": deleted},
        "errors": errors,
        "warnings": warnings,
    }


_KIND_ORDER = {"delete": 0, "create_category": 1, "rename": 2, "create_channel": 3, "move": 4, "reorder": 5}


def compute_diff(snap: dict, clean: dict) -> list:
    """List of human-readable operations needed to turn the live server into the draft."""
    cats_idx, chans_idx = index_snapshot(snap)
    ops = []

    def q(name):
        return f'"{name}"' if name else "no category"

    for d in clean["deleted"]:
        ops.append({"kind": "delete", "id": d["id"], "name": d["name"], "type": d["type"], "text": f'Delete {d["type"]} "{d["name"]}"'})

    def handle(ch, parent_ref, parent_name):
        if ch["id"] is None:
            ops.append({
                "kind": "create_channel", "tmp": ch["tmp"], "name": ch["name"], "type": ch["type"],
                "topic": ch["topic"], "parent": parent_ref, "parent_name": parent_name,
                "text": f'Create {ch["type"]} channel "{ch["name"]}" in {q(parent_name)}',
            })
            return
        orig, orig_parent = chans_idx[ch["id"]]
        if ch["name"] != orig["name"]:
            ops.append({"kind": "rename", "id": ch["id"], "type": ch["type"], "from": orig["name"], "to": ch["name"],
                        "text": f'Rename {ch["type"]} channel "{orig["name"]}" → "{ch["name"]}"'})
        if orig_parent != parent_ref:
            before = cats_idx[orig_parent]["name"] if orig_parent else None
            ops.append({"kind": "move", "id": ch["id"], "name": ch["name"], "from": before, "to": parent_name,
                        "text": f'Move "{ch["name"]}": {q(before)} → {q(parent_name)}'})

    for cat in clean["categories"]:
        if cat["id"] is None:
            ops.append({"kind": "create_category", "tmp": cat["tmp"], "name": cat["name"], "text": f'Create category "{cat["name"]}"'})
        else:
            orig = cats_idx[cat["id"]]
            if cat["name"] != orig["name"]:
                ops.append({"kind": "rename", "id": cat["id"], "type": "category", "from": orig["name"], "to": cat["name"],
                            "text": f'Rename category "{orig["name"]}" → "{cat["name"]}"'})
        ref = cat["id"] or cat["tmp"]
        for ch in cat["channels"]:
            handle(ch, ref, cat["name"])
    for ch in clean["uncategorized"]:
        handle(ch, None, None)

    # Reordering: compare relative order of the items that exist both before and after
    names = {}
    for c in clean["categories"]:
        if c["id"]:
            names[c["id"]] = c["name"]
        for ch in c["channels"]:
            if ch["id"]:
                names[ch["id"]] = ch["name"]
    for ch in clean["uncategorized"]:
        if ch["id"]:
            names[ch["id"]] = ch["name"]

    def order_op(scope, current, final):
        common = set(current) & set(final)
        a = [i for i in current if i in common]
        b = [i for i in final if i in common]
        if a != b:
            ops.append({"kind": "reorder", "scope": scope, "before": [names[i] for i in a], "after": [names[i] for i in b],
                        "text": f"Reorder items in {scope}"})

    order_op(
        "the top level",
        [c["id"] for c in snap["uncategorized"]] + [c["id"] for c in snap["categories"]],
        [c["id"] for c in clean["uncategorized"] if c["id"]] + [c["id"] for c in clean["categories"] if c["id"]],
    )
    for c in clean["categories"]:
        if c["id"]:
            order_op(f'category "{c["name"]}"', [x["id"] for x in cats_idx[c["id"]]["channels"]], [x["id"] for x in c["channels"] if x["id"]])

    ops.sort(key=lambda o: _KIND_ORDER[o["kind"]])
    return ops


def fingerprint(ops: list) -> str:
    stable = [{k: v for k, v in op.items() if k != "text"} for op in ops]
    blob = json.dumps(stable, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(blob).hexdigest()[:20]


def plan_positions(clean: dict, id_map: dict, moved_ids=frozenset(), sync: bool = False) -> list:
    """Final {id, position, parent_id} list for Discord's bulk channel update.

    id_map maps a draft "tmp" key to the real id of something created during apply.
    Items whose creation failed are skipped; their children fall back to the top level.
    """
    def real(item):
        return item["id"] or id_map.get(item["tmp"])

    payload, top = [], 0

    def add_channel(ch, parent_real, position):
        rid = real(ch)
        if not rid:
            return False
        entry = {"id": int(rid), "position": position, "parent_id": int(parent_real) if parent_real else None}
        if sync and ch["id"] in moved_ids:
            entry["lock_permissions"] = True
        payload.append(entry)
        return True

    for ch in clean["uncategorized"]:
        if add_channel(ch, None, top):
            top += 1
    for cat in clean["categories"]:
        crid = real(cat)
        if crid:
            payload.append({"id": int(crid), "position": top})
            top += 1
        local = 0
        for ch in cat["channels"]:
            if crid:
                if add_channel(ch, crid, local):
                    local += 1
            elif add_channel(ch, None, top):
                top += 1
    return payload
