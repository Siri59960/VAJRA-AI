-- VAJRA AI OS project access-control migration.
-- Apply to a verified backup/restore first. Do not run on production until
-- the isolated restore passes all validation checks.

BEGIN;

-- 1. Verify the expected schema and administrator account.
DO $$
BEGIN
    IF to_regclass('public.users') IS NULL
       OR to_regclass('public.projects') IS NULL
       OR to_regclass('public.tasks') IS NULL THEN
        RAISE EXCEPTION
            'Expected users, projects, and tasks tables are required';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM public.users
        WHERE lower(email) = lower('vajra1924@gmail.com')
    ) THEN
        RAISE EXCEPTION
            'Administrator account does not exist in this database';
    END IF;
END
$$;

-- 2. Add project and task ownership fields.
ALTER TABLE public.projects
    ADD COLUMN IF NOT EXISTS owner_id VARCHAR(36);

ALTER TABLE public.projects
    ADD COLUMN IF NOT EXISTS visibility VARCHAR(20)
    NOT NULL DEFAULT 'private';

ALTER TABLE public.tasks
    ADD COLUMN IF NOT EXISTS creator_id VARCHAR(36);

-- 3. Create the membership table if absent.
CREATE TABLE IF NOT EXISTS public.project_members (
    project_id VARCHAR(36) NOT NULL
        REFERENCES public.projects(id) ON DELETE CASCADE,
    user_id VARCHAR(36) NOT NULL
        REFERENCES public.users(id) ON DELETE CASCADE,
    role VARCHAR(20) NOT NULL DEFAULT 'member',
    created_at TIMESTAMP WITH TIME ZONE
        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (project_id, user_id),
    CONSTRAINT project_members_role_check
        CHECK (role IN ('member', 'editor'))
);

-- 4. Repair defaults on an existing membership table.
-- Fail rather than silently modifying any unexpected membership data.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'project_members'
          AND column_name = 'project_id'
    ) OR NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'project_members'
          AND column_name = 'user_id'
    ) OR NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'project_members'
          AND column_name = 'role'
    ) OR NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'project_members'
          AND column_name = 'created_at'
    ) THEN
        RAISE EXCEPTION
            'Existing project_members table has an unexpected schema';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM public.project_members
        WHERE role IS NULL
           OR role NOT IN ('member', 'editor')
    ) THEN
        RAISE EXCEPTION
            'Invalid or NULL membership roles exist; resolve them before migration';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM public.project_members
        WHERE project_id IS NULL
           OR user_id IS NULL
           OR created_at IS NULL
    ) THEN
        RAISE EXCEPTION
            'NULL membership key/timestamp data exists; resolve it before migration';
    END IF;
END
$$;

ALTER TABLE public.project_members
    ALTER COLUMN role SET DEFAULT 'member';

ALTER TABLE public.project_members
    ALTER COLUMN created_at SET DEFAULT CURRENT_TIMESTAMP;

ALTER TABLE public.project_members
    ALTER COLUMN role SET NOT NULL;

ALTER TABLE public.project_members
    ALTER COLUMN created_at SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.project_members'::regclass
          AND conname = 'project_members_role_check'
    ) THEN
        ALTER TABLE public.project_members
            ADD CONSTRAINT project_members_role_check
            CHECK (role IN ('member', 'editor'));
    END IF;
END
$$;

-- 5. Assign existing unowned projects/tasks to the administrator.
DO $$
DECLARE
    admin_id VARCHAR(36);
BEGIN
    SELECT id INTO STRICT admin_id
    FROM public.users
    WHERE lower(email) = lower('vajra1924@gmail.com');

    UPDATE public.projects
    SET owner_id = admin_id
    WHERE owner_id IS NULL;

    UPDATE public.tasks
    SET creator_id = admin_id
    WHERE creator_id IS NULL;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.projects'::regclass
          AND conname = 'projects_owner_id_fkey'
    ) THEN
        ALTER TABLE public.projects
            ADD CONSTRAINT projects_owner_id_fkey
            FOREIGN KEY (owner_id)
            REFERENCES public.users(id) ON DELETE RESTRICT;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.tasks'::regclass
          AND conname = 'tasks_creator_id_fkey'
    ) THEN
        ALTER TABLE public.tasks
            ADD CONSTRAINT tasks_creator_id_fkey
            FOREIGN KEY (creator_id)
            REFERENCES public.users(id) ON DELETE RESTRICT;
    END IF;
END
$$;

-- 6. Add indexes used by ownership queries.
CREATE INDEX IF NOT EXISTS ix_projects_owner_id
    ON public.projects(owner_id);

CREATE INDEX IF NOT EXISTS ix_tasks_creator_id
    ON public.tasks(creator_id);

COMMIT;
