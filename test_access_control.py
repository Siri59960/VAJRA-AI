import os

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import (
    Base,
    Project,
    ProjectMember,
    Task,
    User,
    can_access_project,
    can_access_task,
    require_project_access,
    require_task_access,
)


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)

    with TestSession() as session:
        owner = User(
            id="owner-1",
            email="owner@example.com",
            password_hash="not-used",
        )
        member = User(
            id="member-1",
            email="member@example.com",
            password_hash="not-used",
        )
        outsider = User(
            id="outsider-1",
            email="outsider@example.com",
            password_hash="not-used",
        )
        admin = User(
            id="admin-1",
            email="admin@example.com",
            password_hash="not-used",
        )
        session.add_all([owner, member, outsider, admin])
        session.flush()

        private_project = Project(
            id="private-1",
            name="Private project",
            owner_id=owner.id,
            visibility="private",
        )
        shared_project = Project(
            id="shared-1",
            name="Shared project",
            owner_id=owner.id,
            visibility="shared",
        )
        session.add_all([private_project, shared_project])
        session.flush()

        session.add(
            ProjectMember(
                project_id=shared_project.id,
                user_id=member.id,
                role="member",
            )
        )
        session.add_all([
            Task(
                id="project-task",
                title="Project task",
                project_id=shared_project.id,
                creator_id=owner.id,
            ),
            Task(
                id="personal-task",
                title="Personal task",
                project_id=None,
                creator_id=owner.id,
            ),
        ])
        session.commit()

        yield {
            "session": session,
            "owner": owner,
            "member": member,
            "outsider": outsider,
            "admin": admin,
            "private_project": private_project,
            "shared_project": shared_project,
        }

    engine.dispose()


def test_private_project_is_inaccessible_to_unrelated_user(db):
    session = db["session"]
    project = db["private_project"]

    assert can_access_project(session, project, db["owner"])
    assert not can_access_project(session, project, db["outsider"])

    with pytest.raises(HTTPException) as exc:
        require_project_access(session, project, db["outsider"])
    assert exc.value.status_code == 404


def test_shared_member_can_read_but_cannot_edit(db):
    session = db["session"]
    project = db["shared_project"]
    member = db["member"]

    assert can_access_project(session, project, member)
    require_project_access(session, project, member)

    with pytest.raises(HTTPException) as exc:
        require_project_access(session, project, member, edit=True)
    assert exc.value.status_code == 403


def test_project_editor_can_edit(db):
    session = db["session"]
    project = db["shared_project"]
    member = db["member"]

    membership = session.get(ProjectMember, (project.id, member.id))
    membership.role = "editor"
    session.commit()

    require_project_access(session, project, member, edit=True)


def test_unassigned_task_is_only_accessible_to_creator_or_admin(db):
    session = db["session"]
    task = session.get(Task, "personal-task")

    assert can_access_task(session, task, db["owner"])
    assert not can_access_task(session, task, db["outsider"])

    with pytest.raises(HTTPException) as exc:
        require_task_access(session, task, db["outsider"])
    assert exc.value.status_code == 404


def test_project_task_inherits_project_access(db):
    session = db["session"]
    task = session.get(Task, "project-task")

    assert can_access_task(session, task, db["owner"])
    assert can_access_task(session, task, db["member"])
    assert not can_access_task(session, task, db["outsider"])


def test_configured_admin_can_access_private_project(db):
    session = db["session"]
    project = db["private_project"]
    admin = db["admin"]

    previous = os.environ.get("ADMIN_EMAILS")
    os.environ["ADMIN_EMAILS"] = "admin@example.com"
    try:
        assert can_access_project(session, project, admin)
    finally:
        if previous is None:
            os.environ.pop("ADMIN_EMAILS", None)
        else:
            os.environ["ADMIN_EMAILS"] = previous
