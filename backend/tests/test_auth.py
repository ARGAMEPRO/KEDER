"""Authentication, registration and session-invalidation tests."""
from __future__ import annotations

from app.models import SessionLocal, User


def test_register_requires_matching_passwords(client):
    r = client.post("/api/v1/auth/register", json={"email": "a@b.ru", "password": "GoodPass1",
                                                   "password_confirm": "OtherPass1"})
    assert r.status_code == 422
    assert "не совпадают" in r.json()["detail"]


def test_register_rejects_weak_password(client):
    for pw in ("short1", "nodigits", "12345678"):
        r = client.post("/api/v1/auth/register", json={"email": f"w{pw}@b.ru", "password": pw,
                                                       "password_confirm": pw})
        assert r.status_code == 422, pw


def test_register_success_and_login(client):
    body = {"email": "New.User@Example.RU", "password": "GoodPass1", "password_confirm": "GoodPass1",
            "full_name": "Новый Пользователь"}
    r = client.post("/api/v1/auth/register", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["user"]["role"] == "citizen"          # role is never taken from the client
    assert data["user"]["email"] == "new.user@example.ru"   # normalised
    assert data["user"]["is_demo"] is False
    token = data["access_token"]

    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == "new.user@example.ru"

    login = client.post("/api/v1/auth/login", json={"email": "new.user@example.ru", "password": "GoodPass1"})
    assert login.status_code == 200


def test_register_duplicate_email(client):
    body = {"email": "dup@example.ru", "password": "GoodPass1", "password_confirm": "GoodPass1"}
    assert client.post("/api/v1/auth/register", json=body).status_code == 200
    again = client.post("/api/v1/auth/register", json=body)
    assert again.status_code == 409


def test_register_cannot_choose_role(client):
    r = client.post("/api/v1/auth/register", json={"email": "sneaky@example.ru", "password": "GoodPass1",
                                                   "password_confirm": "GoodPass1", "role": "mchs"})
    assert r.status_code == 200
    with SessionLocal() as db:
        u = db.query(User).filter(User.email == "sneaky@example.ru").one()
    assert u.role.value == "citizen"


def test_login_wrong_password(client, citizen):
    r = client.post("/api/v1/auth/login", json={"email": citizen["email"], "password": "WrongPass1"})
    assert r.status_code == 401


def test_me_requires_token(client):
    assert client.get("/api/v1/auth/me").status_code == 403
    assert client.get("/api/v1/auth/me", headers={"Authorization": "Bearer nonsense"}).status_code == 401


def test_change_password_invalidates_old_token(client, citizen):
    login = client.post("/api/v1/auth/login", json=citizen).json()
    old = {"Authorization": f"Bearer {login['access_token']}"}
    assert client.get("/api/v1/auth/me", headers=old).status_code == 200

    r = client.post("/api/v1/auth/change-password", headers=old,
                    json={"current_password": citizen["password"], "new_password": "BrandNew123",
                          "new_password_confirm": "BrandNew123"})
    assert r.status_code == 200, r.text
    new = {"Authorization": f"Bearer {r.json()['access_token']}"}

    # the old token is dead, the new one works
    assert client.get("/api/v1/auth/me", headers=old).status_code == 401
    assert client.get("/api/v1/auth/me", headers=new).status_code == 200
    assert client.post("/api/v1/auth/login", json={"email": citizen["email"],
                                                   "password": "BrandNew123"}).status_code == 200


def test_change_password_requires_current(client, citizen):
    headers = {"Authorization": f"Bearer {client.post('/api/v1/auth/login', json=citizen).json()['access_token']}"}
    r = client.post("/api/v1/auth/change-password", headers=headers,
                    json={"current_password": "NotTheOne1", "new_password": "BrandNew123",
                          "new_password_confirm": "BrandNew123"})
    assert r.status_code == 403


def test_logout_revokes_token(client, citizen):
    headers = {"Authorization": f"Bearer {client.post('/api/v1/auth/login', json=citizen).json()['access_token']}"}
    assert client.post("/api/v1/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 401


def test_deactivated_user_cannot_use_token(client, citizen):
    headers = {"Authorization": f"Bearer {client.post('/api/v1/auth/login', json=citizen).json()['access_token']}"}
    with SessionLocal() as db:
        u = db.query(User).filter(User.email == citizen["email"]).one()
        u.token_version += 1  # simulate an admin-side revoke
        db.commit()
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 401
