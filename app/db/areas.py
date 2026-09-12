"""Areas (workspaces), their settings blob, feature flags."""
from __future__ import annotations
import json
from typing import Any, Optional
from .. import crypto
from .core import FEATURES, _connect, _features_cache, _primary_area, _users, all_area_ids, default_area_features, init


def user_primary_area(user_id: int) -> Optional[int]:
    if user_id in _primary_area:
        return _primary_area[user_id]
    init()
    with _connect() as c:
        row = c.execute(
            "SELECT area_id FROM memberships WHERE user_id=? ORDER BY area_id LIMIT 1",
            (user_id,),
        ).fetchone()
    area = row["area_id"] if row else None
    if area is not None:  # never cache a miss (the user may be mid-creation)
        _primary_area[user_id] = area
    return area


def user_area_ids(user_id: int) -> list[int]:
    init()
    with _connect() as c:
        return [r["area_id"] for r in c.execute(
            "SELECT area_id FROM memberships WHERE user_id=? ORDER BY area_id", (user_id,)).fetchall()]


def get_area(area_id: int) -> Optional[dict[str, Any]]:
    init()
    with _connect() as c:
        row = c.execute("SELECT * FROM areas WHERE id=?", (area_id,)).fetchone()
        if not row:
            return None
        return {"id": row["id"], "name": row["name"], "owner_user_id": row["owner_user_id"],
                "created_at": row["created_at"]}


def get_area_settings_raw(area_id: int) -> dict[str, Any]:
    """The stored settings JSON as-is (secrets still encrypted)."""
    init()
    with _connect() as c:
        row = c.execute("SELECT settings FROM areas WHERE id=?", (area_id,)).fetchone()
    if not row:
        return {}
    try:
        data = json.loads(row["settings"] or "{}")
    except json.JSONDecodeError as exc:
        # corrupt JSON must surface: returning {} would let the next save wipe the area
        raise ValueError(f"settings of area {area_id} are not valid JSON: {exc}") from exc
    return data if isinstance(data, dict) else {}


def get_area_settings(area_id: int) -> dict[str, Any]:
    """An area's settings with every secret decrypted (see :mod:`app.crypto`)."""
    return crypto.decrypt_settings(get_area_settings_raw(area_id))


def save_area_settings(area_id: int, settings: dict[str, Any]) -> None:
    """Persist an area's settings; secret fields are encrypted on the way in.

    A secret the current key cannot decrypt reads as an empty string; when the
    caller hands such an empty value back for a field whose stored cipher text
    is undecryptable, the stored cipher text is kept — a settings save must
    never destroy a token that a corrected key could still recover."""
    init()
    with _connect() as c:
        row = c.execute("SELECT settings FROM areas WHERE id=?", (area_id,)).fetchone()
        previous: dict[str, Any] = {}
        if row:
            try:
                loaded = json.loads(row["settings"] or "{}")
                previous = loaded if isinstance(loaded, dict) else {}
            except json.JSONDecodeError:
                previous = {}
        c.execute("UPDATE areas SET settings=? WHERE id=?",
                  (json.dumps(crypto.encrypt_settings(crypto.keep_undecryptable(settings, previous))), area_id))


def encrypt_existing_settings() -> int:
    """One-shot upgrade: re-save every area whose stored settings still hold a
    plain-text secret. Returns how many areas were rewritten. Idempotent."""
    init()
    rewritten = 0
    for aid in all_area_ids():
        try:
            raw = get_area_settings_raw(aid)
        except ValueError:
            continue                                # corrupt row: leave it for the operator
        if crypto.needs_reencrypt(raw):  # plain text, or readable only with a previous key
            save_area_settings(aid, crypto.decrypt_settings(raw))
            rewritten += 1
    # the TOTP secrets live outside the settings blob: a rotated key must reach them too
    with _connect() as c:
        rows = c.execute("SELECT id, totp_secret FROM users WHERE totp_secret<>''").fetchall()
        for r in rows:
            raw = r["totp_secret"]
            if crypto.is_current(raw):
                continue
            plain = crypto.decrypt(raw)
            if plain:
                c.execute("UPDATE users SET totp_secret=? WHERE id=?", (crypto.encrypt(plain), r["id"]))
                rewritten += 1
    _users.clear()
    return rewritten


def area_owner(area_id: int) -> Optional[int]:
    a = get_area(area_id)
    return a["owner_user_id"] if a else None


def _load_features(area_id: int) -> dict[str, Any]:
    with _connect() as c:
        row = c.execute("SELECT features FROM areas WHERE id=?", (area_id,)).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row["features"] or "{}")
    except json.JSONDecodeError:
        return {}


def get_area_features(area_id: int) -> dict[str, bool]:
    """Effective feature flags for an area (stored values merged over defaults).
    Cached: ``require_feature`` runs on hot API routes."""
    hit = _features_cache.get(area_id)
    if hit is not None:
        return dict(hit)
    init()
    stored = _load_features(area_id)
    merged = default_area_features()
    for key in FEATURES:
        if key in stored:
            merged[key] = bool(stored[key])
    _features_cache[area_id] = dict(merged)
    return merged


def set_area_feature(area_id: int, feature: str, enabled: bool) -> dict[str, bool]:
    if feature not in FEATURES:
        raise ValueError(f"unknown feature: {feature}")
    init()
    _features_cache.pop(area_id, None)
    with _connect() as c:
        row = c.execute("SELECT features FROM areas WHERE id=?", (area_id,)).fetchone()
        stored: dict[str, Any] = {}
        if row:
            try:
                stored = json.loads(row["features"] or "{}")
            except json.JSONDecodeError:
                stored = {}
        stored[feature] = bool(enabled)
        c.execute("UPDATE areas SET features=? WHERE id=?", (json.dumps(stored), area_id))
    return get_area_features(area_id)


def backfill_alert_emails() -> int:
    """Set each area's alert 'Notify email' to its owner's address where unset.

    Makes the per-user default correct for areas created before that behavior
    (or before multi-tenancy), without touching areas where the user chose an
    address. Returns how many areas were updated. Safe to run repeatedly."""
    init()
    updated = 0
    with _connect() as c:
        rows = c.execute(
            "SELECT a.id AS id, a.settings AS settings, u.email AS email "
            "FROM areas a JOIN users u ON u.id = a.owner_user_id").fetchall()
        for r in rows:
            try:
                s = json.loads(r["settings"] or "{}")
            except json.JSONDecodeError:
                s = {}
            if not s.get("alert_email_to") and r["email"]:
                s["alert_email_to"] = r["email"]
                c.execute("UPDATE areas SET settings=? WHERE id=?", (json.dumps(s), r["id"]))
                updated += 1
    return updated
