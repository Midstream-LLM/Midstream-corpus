CREATE TABLE dataset_member_cut (
    release_id bigint NOT NULL,
    version_id bigint NOT NULL,
    start_char int NOT NULL CHECK (start_char >= 0),
    end_char int NOT NULL CHECK (end_char > start_char),
    PRIMARY KEY (release_id, version_id, start_char),
    FOREIGN KEY (release_id, version_id) REFERENCES dataset_member (release_id, version_id)
);
