import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import (
    Base,
    Project,
    ProjectMember,
    Task,
    User,
    app,
    get_current_user,
    get_db,
)


@pytest.fixture()
def api_setup():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)

    with TestSession() as db:
        owner = User(
            id="api-owner",
            email="owner-api@example.com",
            password_hash="unused",
        )
        member = User(
            id="api-member",
            email="member-api@example.com",
            password_hash="unused",
        )
        outsider = User(
            id="api-outsider",
            email="outsider-api@example.com",
            password_hash="unused",
        )
        project = Project(
            id="api-project",
            name="API test project",
            owner_id=owner.id,
            visibility="private",
        )
        db.add_all([owner, member, outsider, project])
        db.commit()

    def override_db():
        session = TestSession()
        try:
            yield session
        finally:
            session.close()

    active_user = {"user": "owner"}

    def override_current_user():
        with TestSession() as session:
            user_id = {
                "owner": "api-owner",
                "member": "api-member",
                "outsider": "api-outsider",
            }[active_user["user"]]
            user = session.get(User, user_id)
            # Return a detached copy to avoid cross-session identity conflicts.
            session.expunge(user)
            return user

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_current_user

    try:
        with TestClient(app) as client:
            yield {
                "client": client,
                "active_user": active_user,
                "engine": engine,
                "TestSession": TestSession,
            }
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)
        engine.dispose()


def test_owner_can_add_member_and_project_becomes_shared(api_setup):
    client = api_setup["client"]
    response = client.post(
        "/api/projects/api-project/members",
        json={"email": "member-api@example.com", "role": "member"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["member"]["role"] == "member"

    with api_setup["TestSession"]() as db:
        project = db.get(Project, "api-project")
        membership = db.get(ProjectMember, ("api-project", "api-member"))
        assert project.visibility == "shared"
        assert membership is not None
        assert membership.role == "member"


def test_non_owner_cannot_add_member(api_setup):
    api_setup["active_user"]["user"] = "member"
    response = api_setup["client"].post(
        "/api/projects/api-project/members",
        json={"email": "outsider-api@example.com", "role": "member"},
    )
    assert response.status_code == 403


def test_invalid_member_role_is_rejected(api_setup):
    response = api_setup["client"].post(
        "/api/projects/api-project/members",
        json={"email": "member-api@example.com", "role": "administrator"},
    )
    assert response.status_code == 422


def test_owner_can_change_member_role(api_setup):
    with api_setup["TestSession"]() as db:
        project = db.get(Project, "api-project")
        project.visibility = "shared"
        db.add(
            ProjectMember(
                project_id="api-project",
                user_id="api-member",
                role="member",
            )
        )
        db.commit()

    response = api_setup["client"].patch(
        "/api/projects/api-project/members/api-member",
        json={"role": "editor"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "editor"

    with api_setup["TestSession"]() as db:
        membership = db.get(ProjectMember, ("api-project", "api-member"))
        assert membership.role == "editor"


def test_non_owner_cannot_remove_member(api_setup):
    with api_setup["TestSession"]() as db:
        db.get(Project, "api-project").visibility = "shared"
        db.add(
            ProjectMember(
                project_id="api-project",
                user_id="api-member",
                role="member",
            )
        )
        db.commit()

    api_setup["active_user"]["user"] = "outsider"
    response = api_setup["client"].delete(
        "/api/projects/api-project/members/api-member"
    )
    assert response.status_code == 403


def test_owner_can_remove_member_and_revoke_membership(api_setup):
    with api_setup["TestSession"]() as db:
        db.get(Project, "api-project").visibility = "shared"
        db.add(
            ProjectMember(
                project_id="api-project",
                user_id="api-member",
                role="member",
            )
        )
        db.commit()

    response = api_setup["client"].delete(
        "/api/projects/api-project/members/api-member"
    )
    assert response.status_code == 200, response.text

    with api_setup["TestSession"]() as db:
        assert db.get(ProjectMember, ("api-project", "api-member")) is None


def add_test_membership(api_setup, role="member"):
    with api_setup["TestSession"]() as db:
        project = db.get(Project, "api-project")
        project.visibility = "shared"
        db.add(
            ProjectMember(
                project_id="api-project",
                user_id="api-member",
                role=role,
            )
        )
        db.add(
            Task(
                id="api-project-task",
                title="Test project task",
                description="Access control test",
                priority="medium",
                status="open",
                project_id="api-project",
                creator_id="api-owner",
            )
        )
        db.commit()


def test_removed_member_cannot_access_project(api_setup):
    add_test_membership(api_setup)

    response = api_setup["client"].delete(
        "/api/projects/api-project/members/api-member"
    )
    assert response.status_code == 200

    api_setup["active_user"]["user"] = "member"
    response = api_setup["client"].get("/api/projects/api-project")

    assert response.status_code == 404


def test_removed_member_cannot_access_project_tasks(api_setup):
    add_test_membership(api_setup)

    response = api_setup["client"].delete(
        "/api/projects/api-project/members/api-member"
    )
    assert response.status_code == 200

    api_setup["active_user"]["user"] = "member"
    response = api_setup["client"].get(
        "/api/tasks",
        params={"project_id": "api-project"},
    )

    assert response.status_code == 200
    assert response.json()["count"] == 0


def test_member_cannot_edit_project_task(api_setup):
    add_test_membership(api_setup, role="member")
    api_setup["active_user"]["user"] = "member"

    response = api_setup["client"].patch(
        "/api/tasks/api-project-task",
        json={"title": "Unauthorized edit"},
    )

    assert response.status_code == 403


def test_member_cannot_complete_project_task(api_setup):
    add_test_membership(api_setup, role="member")
    api_setup["active_user"]["user"] = "member"

    response = api_setup["client"].post(
        "/api/tasks/api-project-task/complete"
    )

    assert response.status_code == 403


def test_editor_can_edit_project_task(api_setup):
    add_test_membership(api_setup, role="editor")
    api_setup["active_user"]["user"] = "member"

    response = api_setup["client"].patch(
        "/api/tasks/api-project-task",
        json={"title": "Editor updated task"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["task"]["title"] == "Editor updated task"


def test_outsider_cannot_access_shared_project(api_setup):
    add_test_membership(api_setup)
    api_setup["active_user"]["user"] = "outsider"

    response = api_setup["client"].get("/api/projects/api-project")

    assert response.status_code == 404


def test_non_owner_cannot_change_member_role(api_setup):
    add_test_membership(api_setup)
    api_setup["active_user"]["user"] = "outsider"

    response = api_setup["client"].patch(
        "/api/projects/api-project/members/api-member",
        json={"role": "editor"},
    )

    assert response.status_code == 403


def test_removing_final_member_resets_project_visibility(api_setup):
    add_test_membership(api_setup)

    response = api_setup["client"].delete(
        "/api/projects/api-project/members/api-member"
    )
    assert response.status_code == 200

    with api_setup["TestSession"]() as db:
        project = db.get(Project, "api-project")
        assert project.visibility == "private"
