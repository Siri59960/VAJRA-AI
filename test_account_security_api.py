import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import Base, User, app, get_current_user, get_db


@pytest.fixture()
def account_api():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)

    with TestSession() as db:
        db.add_all([
            User(
                id="account-admin",
                email="admin-api@example.com",
                password_hash="unused",
            ),
            User(
                id="account-user",
                email="user-api@example.com",
                password_hash="unused",
            ),
        ])
        db.commit()

    active_user = {"id": "account-user"}

    def override_db():
        with TestSession() as session:
            yield session

    def override_current_user():
        with TestSession() as session:
            user = session.get(User, active_user["id"])
            session.expunge(user)
            return user

    old_admin_emails = os.environ.get("ADMIN_EMAILS")
    os.environ["ADMIN_EMAILS"] = "admin-api@example.com"

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_current_user

    try:
        with TestClient(app) as client:
            yield {"client": client, "active_user": active_user, "TestSession": TestSession}
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)
        if old_admin_emails is None:
            os.environ.pop("ADMIN_EMAILS", None)
        else:
            os.environ["ADMIN_EMAILS"] = old_admin_emails
        engine.dispose()


def test_public_registration_is_disabled(account_api):
    response = account_api["client"].post(
        "/api/auth/register",
        json={
            "email": "new-api@example.com",
            "password": "A-strong-test-password-123",
            "invite_code": "unused-test-invite-code",
        },
    )
    assert response.status_code == 403


def test_non_admin_cannot_provision_account(account_api):
    response = account_api["client"].post(
        "/api/admin/users",
        json={
            "email": "new-api@example.com",
            "password": "A-strong-test-password-123",
        },
    )
    assert response.status_code == 403

    with account_api["TestSession"]() as db:
        count = db.scalar(
            select(func.count(User.id)).where(
                User.email == "new-api@example.com"
            )
        )
        assert count == 0


def test_configured_admin_can_provision_account(account_api):
    account_api["active_user"]["id"] = "account-admin"

    response = account_api["client"].post(
        "/api/admin/users",
        json={
            "email": "  New-User-API@example.com  ",
            "password": "A-strong-test-password-123",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["user"]["email"] == "new-user-api@example.com"

    with account_api["TestSession"]() as db:
        user = db.scalar(
            select(User).where(User.email == "new-user-api@example.com")
        )
        assert user is not None
        assert user.password_hash != "A-strong-test-password-123"


def test_provisioning_rejects_duplicate_email(account_api):
    account_api["active_user"]["id"] = "account-admin"

    response = account_api["client"].post(
        "/api/admin/users",
        json={
            "email": "USER-API@example.com",
            "password": "A-strong-test-password-123",
        },
    )
    assert response.status_code == 409
