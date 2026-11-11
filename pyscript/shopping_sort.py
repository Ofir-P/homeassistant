"""Sort a Home Assistant to-do list by supermarket category.

Categories/keywords live in /config/pyscript/shopping_categories.json - the single source
used by this service, the hourly automation and the Claude sort-shopping-list skill.
Items are only MOVED (never re-added), so no "item added" events fire.
"""
import json

CATEGORIES_FILE = "pyscript/shopping_categories.json"


@pyscript_executor
def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@pyscript_compile
def _norm(text):
    finals = {"ך": "כ", "ם": "מ", "ן": "נ", "ף": "פ", "ץ": "צ"}
    out = []
    for ch in text.lower():
        out.append(" " if ch in "-,()/.+*|:;!?[]{}" else finals.get(ch, ch))
    return " ".join("".join(out).split())


@pyscript_compile
def _compile(data):
    kws = []
    for cat in data["categories"]:
        for k in cat["keywords"]:
            n = _norm(k)
            if n:
                kws.append((n + (" " if k.endswith(" ") else ""), cat["rank"], cat["name"], k))
    return kws


@pyscript_compile
def _match(name, kws):
    hay = " " + _norm(name) + " "
    best = None
    for i, (kw, rank, cname, raw) in enumerate(kws):
        pos = hay.find(" " + kw)
        if pos >= 0:
            key = (-pos, len(kw.strip()), -i)
            if best is None or key > best[0]:
                best = (key, rank, cname, raw)
    return None if best is None else (best[1], best[2], best[3])


@pyscript_compile
def _plan(names, kws, unknown_rank):
    rows = []
    for idx, name in enumerate(names):
        m = _match(name or "", kws)
        if m is None:
            rows.append((unknown_rank, idx, None, None))
        else:
            rows.append((m[0], idx, m[1], m[2]))
    rows.sort(key=lambda r: (r[0], r[1]))
    return rows


@service(supports_response="optional")
def shopping_list_sort(entity_id="todo.shopping_list", dry_run=False):
    """Reorder active items of a to-do list by supermarket category (moves only)."""
    data = _read_json(hass.config.path(CATEGORIES_FILE))
    kws = _compile(data)
    unknown_rank = data.get("unknown_rank", 65)
    entity = hass.data["todo"].get_entity(entity_id)
    if entity is None:
        raise ValueError(f"{entity_id} not found")
    items = [it for it in (entity.todo_items or []) if it.status == "needs_action"]
    rows = _plan([it.summary for it in items], kws, unknown_rank)
    new_order = [items[r[1]].uid for r in rows]
    changed = new_order != [it.uid for it in items]
    if changed and not dry_run:
        prev = None
        for uid in new_order:
            entity.async_move_todo_item(uid=uid, previous_uid=prev)
            prev = uid
    return {
        "changed": changed,
        "moved": changed and not dry_run,
        "items": [
            {"summary": items[r[1]].summary, "category": r[2] or "לא מסווג", "keyword": r[3]}
            for r in rows
        ],
        "unmatched": [items[r[1]].summary for r in rows if r[2] is None],
    }
