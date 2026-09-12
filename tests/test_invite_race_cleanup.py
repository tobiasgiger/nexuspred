"""Regression coverage for single-use invite registration races."""
from __future__ import annotations

from app import db


async def test_registration_losing_invite_race_leaves_no_user(admin, anon_client, monkeypatch):
    code = db.create_invite(admin["id"])
    email = "racing-user@example.com"
    before = db.user_count()

    monkeypatch.setattr(db, "consume_invite", lambda invite_code, user_id: False)

    response = await anon_client.post(
        "/register",
        data={
            "code": code,
            "email": email,
            "password": "password123",
            "password2": "password123",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"].endswith("error=invite")
    assert db.get_user_by_email(email) is None
    assert db.user_count() == before
