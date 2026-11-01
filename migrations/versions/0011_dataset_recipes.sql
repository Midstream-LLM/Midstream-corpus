CREATE TABLE dataset_recipe (
    name text PRIMARY KEY,
    body jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
