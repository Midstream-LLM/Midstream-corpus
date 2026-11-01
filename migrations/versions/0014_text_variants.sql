ALTER TABLE document_version ADD COLUMN variant text;
ALTER TABLE document_version ADD CONSTRAINT variant_is_not_current
    CHECK (variant IS NULL OR (NOT is_current AND parent_version_id IS NOT NULL));
CREATE UNIQUE INDEX one_variant_per_input ON document_version (doc_id, variant, parent_version_id)
    WHERE variant IS NOT NULL;
