"""Server-rendered pages (sign-in, setup, register, reset) in the user's
language. The dashboard itself is localised in the browser (static/js/i18n.js);
these pages run before any JavaScript, so the language comes from the
``fb_lang`` cookie the dashboard sets, else from ``Accept-Language``. English
is the source text; German entries live here, a missing one falls back."""
from __future__ import annotations

from typing import Any

LANGS = ("en", "de")

DE = {
    "Sign in · Fluxbridge": "Anmelden · Fluxbridge",
    "Sign in to your area": "Melde dich in deinem Bereich an",
    "Email": "E-Mail",
    "Password": "Passwort",
    "Sign in": "Anmelden",
    "Access is invite-only. Have an invite link? Open it to create your account.":
        "Der Zugang ist nur auf Einladung möglich. Hast du einen Einladungslink? Öffne ihn, um dein Konto zu erstellen.",
    "First-run setup · Fluxbridge": "Ersteinrichtung · Fluxbridge",
    "Welcome to Fluxbridge": "Willkommen bei Fluxbridge",
    "Create the first admin account": "Erstelle das erste Admin-Konto",
    "Confirm password": "Passwort bestätigen",
    "Create admin & continue": "Admin erstellen & weiter",
    "This becomes the admin who can invite other users. Any existing single-user configuration is migrated into this account's area.":
        "Dieses Konto wird zum Admin, der weitere Benutzer einladen kann. Eine vorhandene Einzelbenutzer-Konfiguration wird in den Bereich dieses Kontos übernommen.",
    "Create account · Fluxbridge": "Konto erstellen · Fluxbridge",
    "Create your account": "Erstelle dein Konto",
    "Fluxbridge — your own isolated area": "Fluxbridge — dein eigener, getrennter Bereich",
    "Create account": "Konto erstellen",
    "This invite link is invalid or has already been used. Ask an admin for a new invite.":
        "Dieser Einladungslink ist ungültig oder wurde bereits verwendet. Bitte einen Admin um eine neue Einladung.",
    "Back to sign in": "Zurück zur Anmeldung",
    "Reset password · Fluxbridge": "Passwort zurücksetzen · Fluxbridge",
    "Set a new password": "Neues Passwort setzen",
    "New password": "Neues Passwort",
    "Set password": "Passwort setzen",
    "This reset link is invalid or has expired. Ask an admin for a new one.":
        "Dieser Link ist ungültig oder abgelaufen. Bitte einen Admin um einen neuen.",
    # form errors (app/routers/auth.py)
    "Too many attempts — please wait a minute and try again.": "Zu viele Versuche — bitte eine Minute warten und erneut versuchen.",
    "Wrong email or password.": "E-Mail oder Passwort falsch.",
    "Passwords don't match.": "Die Passwörter stimmen nicht überein.",
    "Password must be at least 8 characters.": "Das Passwort muss mindestens 8 Zeichen haben.",
    "Enter a valid email.": "Bitte eine gültige E-Mail-Adresse eingeben.",
    "This invite is invalid or already used.": "Diese Einladung ist ungültig oder wurde bereits verwendet.",
    "This reset link is invalid or has expired.": "Dieser Link ist ungültig oder abgelaufen.",
}
DICTS: dict[str, dict[str, str]] = {"de": DE, "en": {}}


def language_of(request: Any) -> str:
    """``fb_lang`` cookie first (what the dashboard resolved), else the browser's Accept-Language."""
    cookie = str(request.cookies.get("fb_lang") or "").lower()
    if cookie in LANGS:
        return cookie
    accept = str(request.headers.get("accept-language") or "").lower()
    for part in accept.split(","):
        tag = part.split(";", 1)[0].strip()
        if not tag:
            continue
        if tag.startswith("de"):
            return "de"
        if tag.startswith("en"):
            return "en"
    return "en"


def translator(lang: str):
    d = DICTS.get(lang) or {}

    def t(text: str) -> str:
        return d.get(text, text)
    return t
