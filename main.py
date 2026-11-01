
import os
import logging
from datetime import datetime, timezone, date, timedelta
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import (
    FastAPI,
    HTTPException,
    Query,
    Depends,
    status,
    Request,
)
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import jwt
from pwdlib import PasswordHash

from sqlalchemy import (
    create_engine,
    String,
    Text,
    DateTime,
    Date,
    ForeignKey,
    select,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker, Session
from sqlalchemy.engine import make_url
from google import genai
from google.genai import types

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vajra")

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
API_KEY = os.getenv("GEMINI_API_KEY")
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30
password_hash = PasswordHash.recommended()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

raw_database_url = os.getenv("DATABASE_URL", "").strip()
if raw_database_url.startswith("postgres://"):
    raw_database_url = "postgresql+psycopg://" + raw_database_url[len("postgres://"):]
elif raw_database_url.startswith("postgresql://"):
    raw_database_url = "postgresql+psycopg://" + raw_database_url[len("postgresql://"):]

if not raw_database_url:
    raise RuntimeError(
        "DATABASE_URL is missing. Configure it in your environment or Codespaces secrets."
    )

database_url = make_url(raw_database_url)
engine_options = {"pool_pre_ping": True}
if database_url.drivername.startswith("postgresql"):
    engine_options["pool_size"] = 5
    engine_options["max_overflow"] = 5

engine = create_engine(raw_database_url, **engine_options)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db():
    """Yield a SQLAlchemy session and always close it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="active", nullable=False)
    deadline: Mapped[date | None] = mapped_column(Date, nullable=True)
    owner_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    visibility: Mapped[str] = mapped_column(
        String(20), default="private", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    owner: Mapped["User | None"] = relationship()
    members: Mapped[list["ProjectMember"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    tasks: Mapped[list["Task"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class ProjectMember(Base):
    __tablename__ = "project_members"

    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(20), default="member", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    project: Mapped["Project"] = relationship(back_populates="members")
    user: Mapped["User"] = relationship()


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    title: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    priority: Mapped[str] = mapped_column(String(20), default="medium", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False)
    project_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    creator_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    creator: Mapped["User | None"] = relationship()
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    project: Mapped[Project | None] = relationship(back_populates="tasks")



client = genai.Client(api_key=API_KEY) if API_KEY else None
# ============================================================
# VAJRA AI OS - MAINTENANCE CONFIGURATION
# ============================================================

MAINTENANCE_MODE = (
    os.getenv("MAINTENANCE_MODE", "false").strip().lower() == "true"
)

app = FastAPI(
    title="VAJRA AI OS",
    version="0.2.0",
    description="VAJRA AI operations assistant with PostgreSQL projects/tasks and authenticated shared-team access.",
)

# ============================================================
# VAJRA AI OS - GLOBAL MAINTENANCE MIDDLEWARE
# ============================================================

@app.middleware("http")
async def maintenance_middleware(request: Request, call_next):
    """
    Temporarily block application API writes during maintenance.

    Read-only GET and HEAD requests remain available.
    Health monitoring remains available.
    """

    if not MAINTENANCE_MODE:
        return await call_next(request)

    # Always allow health monitoring.
    if request.url.path == "/health":
        return await call_next(request)

    # Block all API write operations during maintenance.
    if request.url.path.startswith("/api/") and request.method in {
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
    }:
        return JSONResponse(
            status_code=503,
            content={
                "status": "maintenance",
                "service": "VAJRA AI OS",
                "message": (
                    "VAJRA AI OS is temporarily undergoing "
                    "scheduled maintenance. Please try again later."
                ),
            },
            headers={
                "Retry-After": "300",
            },
        )

    return await call_next(request)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _require_jwt_secret() -> str:
    if not JWT_SECRET_KEY or len(JWT_SECRET_KEY) < 32:
        raise RuntimeError("Set JWT_SECRET_KEY to a random secret of at least 32 characters.")
    return JWT_SECRET_KEY


def create_access_token(user_id: str) -> str:
    secret = _require_jwt_secret()
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {"sub": user_id, "iat": now, "exp": now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)},
        secret,
        algorithm=JWT_ALGORITHM,
    )


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    secret = _require_jwt_secret()
    try:
        payload = jwt.decode(token, secret, algorithms=[JWT_ALGORITHM], options={"require": ["sub", "exp"]})
        user_id = payload.get("sub")
        if not isinstance(user_id, str) or not user_id:
            raise jwt.InvalidTokenError("Missing subject")
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None

    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User no longer exists",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def is_admin(user: User) -> bool:
    """Return whether the user's email is configured as an administrator."""
    configured = {
        email.strip().lower()
        for email in os.getenv("ADMIN_EMAILS", "").split(",")
        if email.strip()
    }
    return user.email.strip().lower() in configured


def can_access_project(db: Session, project: Project, user: User) -> bool:
    """Administrators, owners, and explicit members can access a project."""
    if is_admin(user) or project.owner_id == user.id:
        return True

    if project.visibility == "shared":
        membership = db.get(ProjectMember, (project.id, user.id))
        return membership is not None

    return False


def require_project_access(
    db: Session,
    project: Project,
    user: User,
    *,
    edit: bool = False,
) -> None:
    """Raise HTTP 404 for inaccessible projects and 403 for read-only members."""
    if not can_access_project(db, project, user):
        raise HTTPException(status_code=404, detail="Project not found.")

    if edit and not is_admin(user) and project.owner_id != user.id:
        membership = db.get(ProjectMember, (project.id, user.id))
        if membership is None or membership.role != "editor":
            raise HTTPException(
                status_code=403,
                detail="You do not have permission to edit this project.",
            )


def can_access_task(db: Session, task: Task, user: User) -> bool:
    """Tasks inherit access from their project; unassigned tasks use creator ownership."""
    if is_admin(user):
        return True

    if task.project_id:
        project = db.get(Project, task.project_id)
        return project is not None and can_access_project(db, project, user)

    return task.creator_id == user.id


def require_task_access(
    db: Session,
    task: Task,
    user: User,
    *,
    edit: bool = False,
) -> None:
    """Reject task operations that violate the task's access policy."""
    if not can_access_task(db, task, user):
        raise HTTPException(status_code=404, detail="Task not found.")

    if edit and task.project_id:
        project = db.get(Project, task.project_id)
        if project is not None:
            require_project_access(db, project, user, edit=True)


# ---------- Request schemas ----------

class RegisterRequest(BaseModel):
    email: str = Field(min_length=5, max_length=320)
    password: str = Field(min_length=12, max_length=128)
    invite_code: str = Field(min_length=16, max_length=256)


class ProvisionUserRequest(BaseModel):
    email: str = Field(min_length=5, max_length=320)
    password: str = Field(min_length=12, max_length=128)


class LoginRequest(BaseModel):
    email: str = Field(min_length=5, max_length=320)
    password: str = Field(min_length=1, max_length=128)


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=5000)
    priority: str = "medium"
    project_id: str | None = None


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    priority: str | None = None
    status: str | None = None
    project_id: str | None = None


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=5000)
    status: str = "active"
    deadline: date | None = None


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    status: str | None = None
    deadline: date | None = None



class ProjectMemberRequest(BaseModel):
    email: str = Field(min_length=5, max_length=320)
    role: str = "member"



class ProjectMemberRoleUpdate(BaseModel):
    role: str


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=5000)


# ---------- Serialization helpers ----------

def project_dict(project: Project, task_count: int | None = None) -> dict:
    return {
        "id": project.id,
        "name": project.name,
        "description": project.description,
        "status": project.status,
        "deadline": project.deadline.isoformat() if project.deadline else None,
        "created_at": project.created_at.isoformat(),
        "task_count": task_count,
    }


def task_dict(task: Task) -> dict:
    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "priority": task.priority,
        "status": task.status,
        "project_id": task.project_id,
        "project_name": task.project.name if task.project else None,
        "created_at": task.created_at.isoformat(),
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
    }


# ---------- Database-backed tools for Gemini ----------

def create_task(
    title: str,
    description: str = "",
    priority: str = "medium",
    project_id: str = "",
    current_user: User | None = None,
    db: Session | None = None,
) -> dict:
    """Create a task owned by the authenticated user."""
    if current_user is None:
        return {"success": False, "error": "Authenticated user is required."}
    if db is None:
        return {"success": False, "error": "Database session is required."}

    title = title.strip()
    if not title:
        return {"success": False, "error": "Task title cannot be empty."}

    priority = priority.lower().strip()
    if priority not in {"low", "medium", "high"}:
        priority = "medium"

    project_id = project_id.strip() or None

    if project_id:
        project = db.get(Project, project_id)
        if project is None:
            return {"success": False, "error": "Project ID was not found."}
        try:
            require_project_access(db, project, current_user, edit=True)
        except HTTPException as exc:
            return {"success": False, "error": exc.detail}

    task = Task(
        title=title,
        description=description.strip(),
        priority=priority,
        status="open",
        project_id=project_id,
        creator_id=current_user.id,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return {"success": True, "task": task_dict(task)}


def list_tasks(
    status: str = "all",
    project_id: str = "",
    current_user: User | None = None,
    db: Session | None = None,
) -> dict:
    """List only tasks the authenticated user may access."""
    if current_user is None:
        return {"count": 0, "tasks": [], "error": "Authenticated user is required."}
    if db is None:
        return {"count": 0, "tasks": [], "error": "Database session is required."}

    status = status.lower().strip()
    if status not in {"all", "open", "completed"}:
        status = "all"

    stmt = select(Task).order_by(Task.created_at.desc())
    if status != "all":
        stmt = stmt.where(Task.status == status)
    if project_id.strip():
        stmt = stmt.where(Task.project_id == project_id.strip())

    rows = db.scalars(stmt).all()
    visible = [task for task in rows if can_access_task(db, task, current_user)]
    return {"count": len(visible), "tasks": [task_dict(task) for task in visible]}


def create_project(
    name: str,
    description: str = "",
    deadline: str = "",
    current_user: User | None = None,
    db: Session | None = None,
) -> dict:
    """Create a private project owned by the authenticated user."""
    if current_user is None:
        return {"success": False, "error": "Authenticated user is required."}
    if db is None:
        return {"success": False, "error": "Database session is required."}

    name = name.strip()
    if not name:
        return {"success": False, "error": "Project name cannot be empty."}

    parsed_deadline = None
    if deadline.strip():
        try:
            parsed_deadline = date.fromisoformat(deadline.strip())
        except ValueError:
            return {"success": False, "error": "Deadline must use YYYY-MM-DD."}

    project = Project(
        name=name,
        description=description.strip(),
        status="active",
        deadline=parsed_deadline,
        owner_id=current_user.id,
        visibility="private",
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return {"success": True, "project": project_dict(project, 0)}


def list_projects(
    status: str = "all",
    current_user: User | None = None,
    db: Session | None = None,
) -> dict:
    """List only projects accessible to the authenticated user."""
    if current_user is None:
        return {"count": 0, "projects": [], "error": "Authenticated user is required."}
    if db is None:
        return {"count": 0, "projects": [], "error": "Database session is required."}

    status = status.lower().strip()
    allowed = {"all", "active", "completed", "on_hold"}
    if status not in allowed:
        status = "all"

    stmt = select(Project).order_by(Project.created_at.desc())
    if status != "all":
        stmt = stmt.where(Project.status == status)

    projects = db.scalars(stmt).all()
    visible = [
        project for project in projects
        if can_access_project(db, project, current_user)
    ]

    results = []
    for project in visible:
        count = db.scalar(
            select(func.count(Task.id)).where(Task.project_id == project.id)
        ) or 0
        results.append(project_dict(project, count))

    return {"count": len(results), "projects": results}


# ---------- Authentication API ----------

@app.post("/api/auth/register", status_code=status.HTTP_403_FORBIDDEN)
def register_disabled():
    """Public account registration is disabled; accounts require administrator provisioning."""
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Public registration is disabled. Contact a VAJRA administrator.",
    )


@app.post("/api/admin/users", status_code=status.HTTP_201_CREATED)
def provision_user(
    request: ProvisionUserRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Allow configured administrators to create accounts."""
    if not is_admin(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator permission required.",
        )

    email = request.email.strip().lower()
    if not __import__("re").fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(status_code=422, detail="Enter a valid email address.")

    existing = db.scalar(select(User).where(func.lower(User.email) == email))
    if existing is not None:
        raise HTTPException(status_code=409, detail="Unable to create this account.")

    user = User(
        email=email,
        password_hash=password_hash.hash(request.password),
    )
    db.add(user)
    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Administrator account provisioning failed")
        raise HTTPException(
            status_code=409,
            detail="Unable to create this account.",
        ) from None

    db.refresh(user)
    return {
        "success": True,
        "user": {
            "id": user.id,
            "email": user.email,
            "created_at": user.created_at.isoformat(),
        },
    }


@app.post("/api/auth/login")
def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    """Exchange OAuth2 form credentials for a bearer access token.

    Swagger sends the email address in the OAuth2 `username` field.
    """
    if not JWT_SECRET_KEY or len(JWT_SECRET_KEY) < 32:
        raise HTTPException(
            status_code=503,
            detail="Authentication is not configured securely.",
        )

    email = form_data.username.strip().lower()
    if not __import__("re").fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = db.scalar(select(User).where(func.lower(User.email) == email))
    if user is None or not password_hash.verify(form_data.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = create_access_token(user.id)
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        "user": {"id": user.id, "email": user.email},
    }


@app.get("/api/auth/me")
def auth_me(current_user: User = Depends(get_current_user)):
    return {"id": current_user.id, "email": current_user.email, "created_at": current_user.created_at.isoformat()}


# ---------- Health and root ----------

@app.get("/")
def home():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health():
    database_ok = False
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
        database_ok = True
    except Exception:
        logger.exception("Database health check failed")

    return {
        "status": "ok" if database_ok else "degraded",
        "service": "VAJRA AI OS",
        "ai_configured": client is not None,
        "gemini_configured": client is not None,
        "database_configured": True,
        "database_connected": database_ok,
        "model": MODEL_NAME,
        "storage": "PostgreSQL",
    }


# ---------- Task REST API ----------

@app.get("/api/tasks")
def get_tasks(
    status: str = Query(default="all"),
    project_id: str | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if status not in {"all", "open", "completed"}:
        raise HTTPException(status_code=400, detail="Invalid task status.")
    return list_tasks(
        status,
        project_id or "",
        current_user=current_user,
        db=db,
    )


@app.post("/api/tasks")
def add_task(
    request: TaskCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    result = create_task(
        title=request.title,
        description=request.description,
        priority=request.priority,
        project_id=request.project_id or "",
        current_user=current_user,
        db=db,
    )
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result

@app.patch("/api/tasks/{task_id}")
def update_task(
    task_id: str,
    request: TaskUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    allowed_priorities = {"low", "medium", "high"}
    allowed_statuses = {"open", "completed"}

    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    require_task_access(db, task, current_user, edit=True)

    values = request.model_dump(exclude_unset=True)

    if "title" in values and values["title"] is not None:
        if not values["title"].strip():
            raise HTTPException(status_code=400, detail="Title cannot be empty.")
        task.title = values["title"].strip()

    if "description" in values and values["description"] is not None:
        task.description = values["description"].strip()

    if "priority" in values and values["priority"] is not None:
        if values["priority"] not in allowed_priorities:
            raise HTTPException(status_code=400, detail="Invalid priority.")
        task.priority = values["priority"]

    if "status" in values and values["status"] is not None:
        if values["status"] not in allowed_statuses:
            raise HTTPException(status_code=400, detail="Invalid status.")
        task.status = values["status"]
        task.completed_at = (
            datetime.now(timezone.utc) if task.status == "completed" else None
        )

    if "project_id" in values:
        new_project_id = values["project_id"]
        if new_project_id:
            new_project = db.get(Project, new_project_id)
            if new_project is None:
                raise HTTPException(status_code=404, detail="Project not found.")
            require_project_access(db, new_project, current_user, edit=True)
        task.project_id = new_project_id

    db.commit()
    db.refresh(task)
    return {"success": True, "task": task_dict(task)}

@app.post("/api/tasks/{task_id}/complete")
def mark_task_complete(
    task_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    require_task_access(db, task, current_user, edit=True)

    task.status = "completed"
    task.completed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(task)
    return {"success": True, "task": task_dict(task)}

# ---------- Project REST API ----------

@app.get("/api/projects")
def get_projects(
    status: str = "all",
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if status not in {"all", "active", "completed", "on_hold"}:
        raise HTTPException(status_code=400, detail="Invalid project status.")
    return list_projects(status, current_user=current_user, db=db)

@app.post("/api/projects")
def add_project(
    request: ProjectCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project_status = request.status.lower().strip()
    if project_status not in {"active", "completed", "on_hold"}:
        raise HTTPException(status_code=400, detail="Invalid project status.")

    project = Project(
        name=request.name.strip(),
        description=request.description.strip(),
        status=project_status,
        deadline=request.deadline,
        owner_id=current_user.id,
        visibility="private",
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return {"success": True, "project": project_dict(project, 0)}

@app.get("/api/projects/{project_id}")
def get_project(
    project_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")
    require_project_access(db, project, current_user)

    count = db.scalar(
        select(func.count(Task.id)).where(Task.project_id == project_id)
    ) or 0
    return project_dict(project, count)

@app.patch("/api/projects/{project_id}")
def update_project(
    project_id: str,
    request: ProjectUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")
    require_project_access(db, project, current_user, edit=True)

    values = request.model_dump(exclude_unset=True)

    if "name" in values and values["name"] is not None:
        if not values["name"].strip():
            raise HTTPException(status_code=400, detail="Name cannot be empty.")
        project.name = values["name"].strip()

    if "description" in values and values["description"] is not None:
        project.description = values["description"].strip()

    if "status" in values and values["status"] is not None:
        if values["status"] not in {"active", "completed", "on_hold"}:
            raise HTTPException(status_code=400, detail="Invalid project status.")
        project.status = values["status"]

    if "deadline" in values:
        project.deadline = values["deadline"]

    db.commit()
    db.refresh(project)
    count = db.scalar(
        select(func.count(Task.id)).where(Task.project_id == project_id)
    ) or 0
    return {"success": True, "project": project_dict(project, count)}

@app.post("/api/projects/{project_id}/members")
def add_project_member(
    project_id: str,
    request: ProjectMemberRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Add an existing user to a project or update their role."""
    import re

    role = request.role.strip().lower()
    if role not in {"member", "editor"}:
        raise HTTPException(status_code=422, detail="Role must be member or editor.")

    email = request.email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(status_code=422, detail="Enter a valid email address.")

    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    if not is_admin(current_user) and project.owner_id != current_user.id:
        raise HTTPException(
            status_code=403,
            detail="Only the project owner or an administrator can manage members.",
        )

    member = db.scalar(select(User).where(func.lower(User.email) == email))
    if member is None:
        raise HTTPException(status_code=404, detail="User account not found.")
    if member.id == project.owner_id:
        raise HTTPException(status_code=400, detail="The project owner already has access.")

    membership = db.get(ProjectMember, (project.id, member.id))
    if membership is None:
        db.add(ProjectMember(project_id=project.id, user_id=member.id, role=role))
    else:
        membership.role = role

    project.visibility = "shared"
    db.commit()
    return {
        "success": True,
        "project_id": project.id,
        "member": {"id": member.id, "email": member.email, "role": role},
    }


@app.patch("/api/projects/{project_id}/members/{user_id}")
def update_project_member(
    project_id: str,
    user_id: str,
    request: ProjectMemberRoleUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Change a project member's role."""
    role = request.role.strip().lower()
    if role not in {"member", "editor"}:
        raise HTTPException(status_code=422, detail="Role must be member or editor.")

    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    if not is_admin(current_user) and project.owner_id != current_user.id:
        raise HTTPException(
            status_code=403,
            detail="Only the project owner or an administrator can manage members.",
        )

    membership = db.get(ProjectMember, (project.id, user_id))
    if membership is None:
        raise HTTPException(status_code=404, detail="Project member not found.")

    membership.role = role
    db.commit()
    return {
        "success": True,
        "project_id": project.id,
        "user_id": user_id,
        "role": role,
    }


@app.delete("/api/projects/{project_id}/members/{user_id}")
def remove_project_member(
    project_id: str,
    user_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Remove a member and revoke their project access."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    if not is_admin(current_user) and project.owner_id != current_user.id:
        raise HTTPException(
            status_code=403,
            detail="Only the project owner or an administrator can manage members.",
        )

    membership = db.get(ProjectMember, (project.id, user_id))
    if membership is None:
        raise HTTPException(status_code=404, detail="Project member not found.")

    db.delete(membership)
    db.flush()

    remaining_members = db.scalar(
        select(func.count())
        .select_from(ProjectMember)
        .where(ProjectMember.project_id == project.id)
    ) or 0

    if remaining_members == 0:
        project.visibility = "private"

    db.commit()
    return {
        "success": True,
        "project_id": project.id,
        "removed_user_id": user_id,
    }


# ---------- Gemini chat: controlled tool execution ----------

GEMINI_TOOL_DECLARATIONS = [
    types.FunctionDeclaration(
        name="create_task",
        description="Create a task in the shared VAJRA workspace.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "title": types.Schema(type=types.Type.STRING),
                "description": types.Schema(type=types.Type.STRING),
                "priority": types.Schema(type=types.Type.STRING),
                "project_id": types.Schema(type=types.Type.STRING),
            },
            required=["title"],
        ),
    ),
    types.FunctionDeclaration(
        name="list_tasks",
        description="List tasks in the shared VAJRA workspace.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "status": types.Schema(type=types.Type.STRING),
                "project_id": types.Schema(type=types.Type.STRING),
            },
        ),
    ),
    types.FunctionDeclaration(
        name="create_project",
        description="Create a project in the shared VAJRA workspace.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "name": types.Schema(type=types.Type.STRING),
                "description": types.Schema(type=types.Type.STRING),
                "deadline": types.Schema(type=types.Type.STRING),
            },
            required=["name"],
        ),
    ),
    types.FunctionDeclaration(
        name="list_projects",
        description="List projects in the shared VAJRA workspace.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "status": types.Schema(type=types.Type.STRING),
            },
        ),
    ),
]

GEMINI_TOOLS = [
    types.Tool(function_declarations=GEMINI_TOOL_DECLARATIONS)
]


def execute_gemini_tool(
    name: str,
    args: dict,
    current_user: User,
    db: Session,
) -> dict:
    """Execute approved Gemini tools in the authenticated user's access scope."""
    logger.info(
        "Gemini tool request: user_id=%s tool=%s",
        current_user.id,
        name,
    )

    if not isinstance(args, dict):
        return {"success": False, "error": "Invalid tool arguments."}

    allowed_arguments = {
        "create_task": {"title", "description", "priority", "project_id"},
        "list_tasks": {"status", "project_id"},
        "create_project": {"name", "description", "deadline"},
        "list_projects": {"status"},
    }

    if name not in allowed_arguments:
        return {"success": False, "error": "Tool is not permitted."}

    if set(args) - allowed_arguments[name]:
        return {"success": False, "error": "Unexpected tool arguments."}

    try:
        if name == "create_task":
            if not isinstance(args.get("title"), str):
                return {"success": False, "error": "A task title is required."}
            return create_task(
                title=args["title"],
                description=args.get("description", ""),
                priority=args.get("priority", "medium"),
                project_id=args.get("project_id", ""),
                current_user=current_user,
                db=db,
            )

        if name == "list_tasks":
            return list_tasks(
                status=args.get("status", "all"),
                project_id=args.get("project_id", ""),
                current_user=current_user,
                db=db,
            )

        if name == "create_project":
            if not isinstance(args.get("name"), str):
                return {"success": False, "error": "A project name is required."}
            return create_project(
                name=args["name"],
                description=args.get("description", ""),
                deadline=args.get("deadline", ""),
                current_user=current_user,
                db=db,
            )

        if name == "list_projects":
            return list_projects(
                status=args.get("status", "all"),
                current_user=current_user,
                db=db,
            )

    except Exception:
        logger.exception(
            "Gemini tool failed: user_id=%s tool=%s",
            current_user.id,
            name,
        )
        return {"success": False, "error": "The requested operation failed."}

    return {"success": False, "error": "Tool is not permitted."}


@app.post("/api/chat")
def chat(
    request: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="Gemini API key is missing. Configure GEMINI_API_KEY.",
        )

    message = request.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message cannot be empty.")

    system_instruction = """
You are VAJRA AI OS, the operations assistant for Team VAJRA.

The authenticated user operates in the shared VAJRA team workspace.
Use the provided tools for requests to create or list projects and tasks.
Never claim an operation succeeded unless its tool result confirms success.
Ask a concise follow-up when a required project or task name is missing.
Do not claim to access email, files, external websites, or robot telemetry
unless an implemented tool provides that access.
Keep routine answers concise and practical.
"""

    contents = [
        types.Content(
            role="user",
            parts=[types.Part.from_text(text=message)],
        )
    ]

    try:
        # Limit tool rounds to prevent an endless function-call loop.
        for _ in range(4):
            response = client.models.generate_content(
                model=MODEL_NAME,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    tools=GEMINI_TOOLS,
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(
                        disable=True
                    ),
                    max_output_tokens=500,
                ),
            )

            if not response.candidates:
                return {
                    "reply": response.text or "Gemini returned no response.",
                    "model": MODEL_NAME,
                }

            candidate_content = response.candidates[0].content
            if candidate_content is None:
                break

            contents.append(candidate_content)

            function_calls = [
                part.function_call
                for part in (candidate_content.parts or [])
                if part.function_call is not None
            ]

            if not function_calls:
                return {
                    "reply": response.text or "Gemini returned no text.",
                    "model": MODEL_NAME,
                }

            tool_parts = []

            for call in function_calls:
                tool_result = execute_gemini_tool(
                    name=call.name or "",
                    args=dict(call.args or {}),
                    current_user=current_user,
                    db=db,
                )

                tool_parts.append(
                    types.Part.from_function_response(
                        name=call.name or "unknown",
                        response=tool_result,
                    )
                )

            contents.append(
                types.Content(role="user", parts=tool_parts)
            )

        raise HTTPException(
            status_code=502,
            detail="Gemini exceeded the allowed tool-call rounds.",
        )

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Gemini request failed")
        raise HTTPException(
            status_code=502,
            detail="Gemini request failed. Check server logs and API quota.",
        ) from exc
