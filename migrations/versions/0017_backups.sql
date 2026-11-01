CREATE TABLE backup_record (
    backup_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    endpoint text NOT NULL,
    bucket text NOT NULL,
    object_key text NOT NULL,
    database_name text NOT NULL,
    status text NOT NULL CHECK(status IN ('running','completed','failed','deleting','deleted')),
    objects jsonb NOT NULL DEFAULT '[]',
    byte_count bigint,
    actor text NOT NULL,
    reason text NOT NULL,
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    verified_at timestamptz
);
