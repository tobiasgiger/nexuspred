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
    # two-factor pages
    "Two-factor code · Fluxbridge": "Zwei-Faktor-Code · Fluxbridge",
    "Enter the code from your authenticator app": "Gib den Code aus deiner Authenticator-App ein",
    "6-digit code or a backup code": "6-stelliger Code oder ein Backup-Code",
    "Continue": "Weiter",
    "Lost your phone? Use one of your backup codes (each works once). Without phone and codes, an administrator can reset your two-factor setup.":
        "Telefon verloren? Verwende einen deiner Backup-Codes (jeder gilt einmal). Ohne Telefon und Codes kann ein Administrator deine Zwei-Faktor-Einrichtung zurücksetzen.",
    "Set up two-factor authentication · Fluxbridge": "Zwei-Faktor-Authentifizierung einrichten · Fluxbridge",
    "Protect your account": "Schütze dein Konto",
    "Two-factor authentication is required for every account.": "Zwei-Faktor-Authentifizierung ist für jedes Konto Pflicht.",
    "Install an authenticator app (Google Authenticator, Microsoft Authenticator, Authy, 1Password, Aegis …).":
        "Installiere eine Authenticator-App (Google Authenticator, Microsoft Authenticator, Authy, 1Password, Aegis …).",
    "Scan this QR code, or type the key by hand.": "Scanne diesen QR-Code oder tippe den Schlüssel von Hand ein.",
    "Enter the 6-digit code the app shows.": "Gib den 6-stelligen Code ein, den die App anzeigt.",
    "6-digit code": "6-stelliger Code",
    "Activate": "Aktivieren",
    "Account": "Konto",
    "Sign out": "Abmelden",
    "Backup codes · Fluxbridge": "Backup-Codes · Fluxbridge",
    "Two-factor authentication is on": "Zwei-Faktor-Authentifizierung ist aktiv",
    "Save these backup codes now": "Speichere jetzt diese Backup-Codes",
    "Each code signs you in once when your phone is not at hand. They are shown only now — store them in your password manager or print them. You can request a new set under Account at any time; it replaces this one.":
        "Jeder Code meldet dich einmal an, wenn dein Telefon nicht zur Hand ist. Sie werden nur jetzt angezeigt — speichere sie im Passwort-Manager oder drucke sie aus. Unter Konto kannst du jederzeit einen neuen Satz anfordern; er ersetzt diesen.",
    "I have saved them — continue": "Ich habe sie gespeichert — weiter",
    "That code is not valid.": "Dieser Code ist ungültig.",
    "The sign-in expired — enter your password again.": "Die Anmeldung ist abgelaufen — gib dein Passwort erneut ein.",
    "That code is not valid — check the time on your phone and try again.": "Dieser Code ist ungültig — prüfe die Uhrzeit auf deinem Telefon und versuche es erneut.",
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
