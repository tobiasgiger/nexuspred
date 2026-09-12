"""Localisation: server-rendered pages follow the fb_lang cookie / Accept-Language,
the dashboard language setting is validated, the German dictionary is well formed."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from app import config, i18n


async def test_sign_in_page_language(admin, anon_client):
    r = await anon_client.get("/login")
    assert "Sign in to your area" in r.text and 'lang="en"' in r.text
    r = await anon_client.get("/login", headers={"accept-language": "de-CH,de;q=0.9,en;q=0.8"})
    assert "Melde dich in deinem Bereich an" in r.text and 'lang="de"' in r.text and "Passwort" in r.text
    r = await anon_client.get("/login", headers={"accept-language": "de", "cookie": "fb_lang=en"})
    assert "Sign in to your area" in r.text                                   # the dashboard's choice wins
    r = await anon_client.post("/login", data={"email": "admin@example.com", "password": "nope"}, headers={"accept-language": "de"})
    r = await anon_client.get(r.headers["location"], headers={"accept-language": "de"})
    assert "E-Mail oder Passwort falsch." in r.text                           # form errors translated too


def test_language_detection():
    class R:
        def __init__(self, cookies=None, headers=None):
            self.cookies, self.headers = cookies or {}, headers or {}
    assert i18n.language_of(R()) == "en"
    assert i18n.language_of(R(headers={"accept-language": "fr-CH,de;q=0.8"})) == "de"
    assert i18n.language_of(R(headers={"accept-language": "fr"})) == "en"
    assert i18n.language_of(R(cookies={"fb_lang": "de"}, headers={"accept-language": "en"})) == "de"
    assert i18n.language_of(R(cookies={"fb_lang": "xx"})) == "en"


async def test_language_setting_is_validated(client, admin):
    assert (await client.get("/api/settings")).json()["ui_language"] == "auto"
    r = await client.post("/api/settings", json={"ui_language": "de"})
    assert r.status_code == 200 and r.json()["ui_language"] == "de"
    assert (await client.post("/api/settings", json={"ui_language": "fr"})).status_code == 400


def test_german_dictionary_covers_the_ui():
    root = Path(__file__).resolve().parents[1] / "static" / "js"
    keys = set()
    for p in root.rglob("*.js"):
        if "locales" in p.parts or p.name == "i18n.js":
            continue
        for m in re.finditer(r'\bt\(("(?:[^"\\\n]|\\.)*")', p.read_text()):
            keys.add(json.loads(m.group(1)))
    out = subprocess.run(["node", "-e", "import('./static/js/locales/de.js').then(m => console.log(JSON.stringify(m.DE)))"],
                         cwd=root.parents[1], capture_output=True, text=True, check=True)
    de = json.loads(out.stdout)
    untranslated = [k for k in keys if k not in de and re.search(r"[A-Za-z]{2}.*\s", k) and "://" not in k and "|" not in k and "@" not in k]
    assert untranslated == [], untranslated                                     # every sentence-like string has a German entry
    for k, v in de.items():
        assert set(re.findall(r"\{(\w+)\}", k)) == set(re.findall(r"\{(\w+)\}", v)), k   # placeholders survive translation
        assert v.strip(), k
