#!/usr/bin/env python3
"""ReciMe -> Home Assistant recipe-book sync (ADD-ONLY). Runs inside Home Assistant.

Usage:  recime_sync.py check   (read-only: reports what would be added)
        recime_sync.py run     (adds new recipes to the `recipe-book` dashboard)

Credentials are read from /config/secrets.yaml:
    recime_email, recime_password          (required)
    recime_ha_token                        (optional: HA long-lived token; only needed if the
                                            Supervisor token is not accepted for the dashboard write)

Prints exactly ONE JSON line on stdout. Never prints credentials or tokens.
"""
import asyncio
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

FIREBASE_KEY = "AIzaSyC5_jv0YHOC_3OKhDju4Hw6AXLTyIxByXs"  # public web key from recime.app
API = "https://api.recime.app"
CDN = "https://cdn.prod.recime.app/images/"
SECRETS = "/config/secrets.yaml"
BACKUP_DIR = "/config/recime_sync/backups"
HDR = {"Content-Type": "application/json", "Origin": "https://www.recime.app",
       "Referer": "https://www.recime.app/", "User-Agent": "Mozilla/5.0 (HomeAssistant recime-sync)"}

# ReciMe cookbook id -> (dashboard slug, English name, icon, icon colour)
BOOKS = {
    "de662462-7c84-4072-8c5e-9a78c0143558": ("chicken", "Chicken", "mdi:food-drumstick", "amber"),
    "3f923390-2c29-4961-981f-40696825b63e": ("sauces", "Sauces", "mdi:bowl-mix", "red"),
    "23225e9c-ef65-4c28-9bc1-b8c68a6a6f00": ("salads", "Salads", "mdi:leaf", "green"),
    "af1805e2-097a-48f6-b302-9bfa2da682c6": ("ninja-grill", "Ninja Grill", "mdi:grill", "deep-orange"),
    "7b99ef0c-8272-4c26-a35b-a500625c8ec4": ("desserts", "Desserts", "mdi:cake-variant", "pink"),
    "bfb8a25d-42e7-4001-9d63-bb60fa4f26b2": ("fish", "Fish", "mdi:fish", "blue"),
    "29665599-e26c-49da-a0f0-4f49b05c011d": ("italian", "Italian", "mdi:pasta", "red"),
    "7557b7ba-d605-4517-bbad-eb78792f2d34": ("soups", "Soups", "mdi:pot-steam", "orange"),
    "7ef2b2cf-76d6-4d6a-bce4-6b54ea4cec59": ("pickles", "Pickles", "mdi:food-variant", "green"),
    "915434b2-6459-4afd-9596-bc916c32ec22": ("dough", "Dough", "mdi:bread-slice", "brown"),
    "8fef52f2-e05a-4d49-a2fb-cb4687706759": ("sides", "Sides", "mdi:rice", "amber"),
    "fd306b84-7550-4550-a0c1-0b1103f82f7f": ("mexican", "Mexican", "mdi:taco", "orange"),
}
SITE_LABELS = {"facebook.com": "Facebook", "instagram.com": "Instagram",
               "youtube.com": "YouTube", "tiktok.com": "TikTok"}


class SyncError(Exception):
    pass


def read_secrets():
    out = {}
    for line in open(SECRETS, encoding="utf-8"):
        if line.lstrip().startswith("#"):
            continue
        m = re.match(r"^\s*([A-Za-z0-9_]+)\s*:\s*(.*?)\s*$", line)
        if m:
            v = m.group(2)
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            out[m.group(1)] = v
    return out


def http(url, data=None, headers=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={**HDR, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()[:400]
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


def recime_login(secrets):
    email, pw = secrets.get("recime_email"), secrets.get("recime_password")
    if not email or not pw:
        raise SyncError("recime_email / recime_password missing in secrets.yaml")
    st, js = http("https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=" + FIREBASE_KEY,
                  {"email": email, "password": pw, "returnSecureToken": True})
    if st != 200:
        msg = js.get("error", {}).get("message") if isinstance(js, dict) else str(js)[:80]
        raise SyncError(f"ReciMe login failed ({msg}); check recime_email/recime_password in secrets.yaml")
    return {"Authorization": "Bearer " + js["idToken"]}


def api_get(path, auth):
    st, js = http(API + path, headers=auth)
    if st != 200:
        raise SyncError(f"ReciMe API {path.split('?')[0]} returned {st}")
    return js


# ---------------------------------------------------------------- Home Assistant websocket
class HA:
    def __init__(self, secrets):
        self.secrets = secrets
        self.i = 0

    async def __aenter__(self):
        import aiohttp
        self.session = aiohttp.ClientSession()
        token = self.secrets.get("recime_ha_token")
        if token:
            url, how = "ws://127.0.0.1:8123/api/websocket", "recime_ha_token"
        else:
            token, url, how = os.environ.get("SUPERVISOR_TOKEN"), "ws://supervisor/core/websocket", "supervisor"
            if not token:
                await self.session.close()
                raise SyncError("No SUPERVISOR_TOKEN in environment; add recime_ha_token to secrets.yaml")
        self.ws = await self.session.ws_connect(url, heartbeat=30)
        await self.ws.receive_json()  # auth_required
        await self.ws.send_json({"type": "auth", "access_token": token})
        msg = await self.ws.receive_json()
        if msg.get("type") != "auth_ok":
            await self.session.close()
            raise SyncError(f"Home Assistant websocket auth failed via {how}; "
                            "create a long-lived token and add it as recime_ha_token in secrets.yaml")
        self.how = how
        return self

    async def __aexit__(self, *a):
        await self.session.close()

    async def cmd(self, typ, **kw):
        self.i += 1
        await self.ws.send_json({"id": self.i, "type": typ, **kw})
        while True:
            m = await self.ws.receive_json()
            if m.get("id") == self.i and m.get("type") == "result":
                if not m.get("success"):
                    raise SyncError(f"HA command {typ} failed: {str(m.get('error'))[:150]}")
                return m.get("result")


# ---------------------------------------------------------------- dashboard builders (mirror of the /recime-sync lambdas)
def clean(t):
    return re.sub(r"\s+", " ", (t or "").replace('"', "״")).strip()


def rv(pid8, title, cb, img, servings, ing, ins, src):
    md = ""
    if img:
        md += f"![]({CDN}original/{img})\n\n"
    if servings:
        md += f"**Servings:** {servings}\n\n"
    if ing:
        md += "### Ingredients\n- " + "\n- ".join(ing) + "\n\n"
    if ins:
        md += "### Instructions\n" + "\n".join(f"{i + 1}. {x}" for i, x in enumerate(ins))
    if src:
        md += "\n\n" + src
    return {"title": title, "path": f"{cb}-{pid8}", "type": "sections", "subview": True,
            "back_path": f"/recipe-book/{cb}", "max_columns": 1,
            "sections": [{"type": "grid", "cards": [{"type": "markdown", "content": md, "card_mod": {"style": {
                ".": "ha-card { direction: rtl; text-align: right; }",
                "ha-markdown": {"$": "img { max-width: 100%; border-radius: 12px; margin-bottom: 8px; }"}}}}]}]}


def tile(pid8, title, secondary, img, cb):
    t = {"type": "custom:mushroom-template-card", "primary": title, "secondary": secondary,
         "layout": "horizontal", "card_mod": {"style": "ha-card { direction: rtl; }"},
         "tap_action": {"action": "navigate", "navigation_path": f"/recipe-book/{cb}-{pid8}"}}
    if img:
        t["picture"] = f"{CDN}thumbnail/{img}"
    return t


def cbv(title, path, tiles):
    return {"title": title, "path": path, "type": "sections", "subview": True,
            "back_path": "/recipe-book/home", "max_columns": 2,
            "sections": [{"type": "grid", "cards": [{"type": "grid", "columns": 1, "square": False, "cards": tiles}]}]}


def site_of(url):
    if not url:
        return "", ""
    host = re.sub(r"^www\.", "", urllib.parse.urlparse(url).hostname or "")
    return host, SITE_LABELS.get(host, host)


def build_recipe(post):
    ing = []
    for x in sorted(post.get("ingredients") or [], key=lambda z: z.get("position", 0)):
        t = clean(x.get("heading") or x.get("rawText") or x.get("preProcessedText"))
        if t:
            ing.append(t)
    ins = [clean(x.get("text")) for x in sorted(post.get("instructions") or [], key=lambda z: z.get("position", 0))
           if clean(x.get("text"))]
    img_url = post.get("imageUrl") or ((post.get("imageUrls") or [None])[0])
    img = os.path.basename(img_url.split("?")[0]) if img_url else None
    url = post.get("recipeUrl") or post.get("sourceUrl") or ""
    host, label = site_of(url)
    src = f"[Source: {label or host}]({url})" if url else ""
    servings = str(post["servingSize"]) if post.get("servingSize") else ""
    return {"title": clean(post.get("title")) or "Recipe", "img": img, "servings": servings,
            "ing": ing, "ins": ins, "src": src, "secondary": host}


def recipe_counts(views):
    per, ids = {}, set()
    for v in views:
        p, back = v.get("path", ""), v.get("back_path", "")
        if p == "home" or back in ("", "/recipe-book/home"):
            continue
        slug, _, id8 = p.rpartition("-")
        per[slug] = per.get(slug, 0) + 1
        ids.add(id8)
    return per, ids


def plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


# ---------------------------------------------------------------- main
async def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    res = {"status": "ok", "mode": mode, "new": [], "new_cookbooks": [], "message": ""}
    secrets = read_secrets()
    auth = recime_login(secrets)
    cookbooks = [c for c in api_get("/v5/cookbooks?sort=latest", auth) if c.get("type") != "UNCATEGORIZED"]

    async with HA(secrets) as ha:
        res["ha_auth"] = ha.how
        rb = await ha.cmd("lovelace/config", url_path="recipe-book")
        views = rb["views"]
        if not views or views[0].get("path") != "home":
            raise SyncError("recipe-book dashboard does not look as expected (no `home` view); aborting")
        existing = {v.get("path") for v in views}

        todo = []  # (cookbook, slug, english name, post id)
        for cb in cookbooks:
            info = BOOKS.get(cb["id"])
            slug = info[0] if info else "cb-" + cb["id"][:8]
            name = info[1] if info else cb.get("title", slug)
            for pid in cb.get("posts") or []:
                if f"{slug}-{pid[:8]}" not in existing:
                    todo.append((cb, slug, name, pid))
        res["pending"] = len(todo)
        res["cookbooks_total"] = len(cookbooks)
        if mode != "run" or not todo:
            res["new"] = [{"title": "?", "cookbook": t[2], "id": t[3][:8]} for t in todo] if mode != "run" else []
            return res

        # --- fetch recipe details and build views
        home_tiles = views[0]["sections"][0]["cards"][2]["cards"]
        touched = set()
        for cb, slug, name, pid in todo:
            post = api_get(f"/v1/posts?id={pid}", auth)
            r = build_recipe(post)
            cb_view = next((v for v in views if v.get("path") == slug), None)
            if cb_view is None:
                info = BOOKS.get(cb["id"])
                icon, color = (info[2], info[3]) if info else ("mdi:silverware-fork-knife", "amber")
                views.append(cbv(cb.get("title", slug), slug, []))
                cb_view = views[-1]
                home_tiles.append({"type": "custom:mushroom-template-card", "primary": cb.get("title", slug),
                                   "secondary": "0 recipes", "icon": icon, "icon_color": color, "layout": "vertical",
                                   "tap_action": {"action": "navigate", "navigation_path": f"/recipe-book/{slug}"}})
                res["new_cookbooks"].append(cb.get("title", slug))
            p8 = pid[:8]
            views.append(rv(p8, r["title"], slug, r["img"], r["servings"], r["ing"], r["ins"], r["src"]))
            cb_view["sections"][0]["cards"][0]["cards"].append(tile(p8, r["title"], r["secondary"], r["img"], slug))
            existing.add(f"{slug}-{p8}")
            touched.add(slug)
            res["new"].append({"title": r["title"], "cookbook": name, "id": p8})

        # --- counts
        per, ids = recipe_counts(views)
        for t in home_tiles:
            slug = (t.get("tap_action", {}).get("navigation_path") or "").replace("/recipe-book/", "")
            if slug in touched:
                t["secondary"] = plural(per.get(slug, 0), "recipe")
        total, books = len(ids), len({s for s, n in per.items() if n})

        # --- backup then save recipe-book
        os.makedirs(BACKUP_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        json.dump(rb, open(f"{BACKUP_DIR}/recipe-book-{stamp}.json", "w", encoding="utf-8"), ensure_ascii=False)
        for old in sorted(os.listdir(BACKUP_DIR))[:-10]:
            os.remove(os.path.join(BACKUP_DIR, old))
        await ha.cmd("lovelace/config/save", url_path="recipe-book", config=rb)

        # --- Kitchen button on the mobile dashboard
        mob = await ha.cmd("lovelace/config", url_path="dashboard-mobile")
        k = next((v for v in mob["views"] if v.get("path") == "kitchen"), None)
        changed = False
        if k:
            for c in k["cards"][1]["cards"]:
                if c.get("primary") == "Recipes":
                    c["secondary"] = f"{plural(total, 'recipe')} · {plural(books, 'cookbook')}"
                    changed = True
        if changed:
            await ha.cmd("lovelace/config/save", url_path="dashboard-mobile", config=mob)
        res["totals"] = {"recipes": total, "cookbooks": books}

    n = len(res["new"])
    items = [f"{x['title']} ({x['cookbook']})" for x in res["new"]]
    shown = ", ".join(items[:3]) + (f" +{n - 3} more" if n > 3 else "")
    msg = ("New recipe: " if n == 1 else f"{n} new recipes: ") + shown
    if res["new_cookbooks"]:
        msg += " · New cookbook: " + ", ".join(res["new_cookbooks"])
    res["message"] = msg
    return res


if __name__ == "__main__":
    try:
        out = asyncio.run(main())
        print(json.dumps(out, ensure_ascii=False))
    except SyncError as e:
        print(json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False))
        sys.exit(1)
    except Exception as e:  # never leak secrets in tracebacks
        print(json.dumps({"status": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"}, ensure_ascii=False))
        sys.exit(1)
