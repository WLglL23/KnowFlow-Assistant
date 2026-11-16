-- KnowFlow fresh-install schema, equivalent to revision 20260822_0009.
-- Import into an empty MySQL 8.4 database. Accounts are seeded by API startup.
SET NAMES utf8mb4;

CREATE TABLE accounts (
	id VARCHAR(36) NOT NULL,
	username VARCHAR(255) NOT NULL,
	password_hash VARCHAR(255),
	display_name VARCHAR(255) NOT NULL,
	`role` ENUM('ADMIN','EMPLOYEE') NOT NULL,
	is_guest BOOL NOT NULL,
	created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
	PRIMARY KEY (id),
	UNIQUE (username)
)CHARSET=utf8mb4 ENGINE=InnoDB COLLATE utf8mb4_0900_ai_ci;

CREATE TABLE alembic_version (
	version_num VARCHAR(32) NOT NULL,
	CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
)CHARSET=utf8mb4 ENGINE=InnoDB COLLATE utf8mb4_0900_ai_ci;

CREATE TABLE conversations (
	id VARCHAR(37) NOT NULL,
	owner_account_id VARCHAR(36) NOT NULL,
	title VARCHAR(100) NOT NULL,
	knowledge_mode ENUM('DISABLED','ALL','SELECTED') NOT NULL,
	selected_source_ids_json JSON NOT NULL,
	created_at DATETIME(6) NOT NULL,
	updated_at DATETIME(6) NOT NULL,
	summary_text TEXT,
	summary_through_message_id VARCHAR(36),
	deleted_at DATETIME(6),
	PRIMARY KEY (id),
	FOREIGN KEY(owner_account_id) REFERENCES accounts (id)
)CHARSET=utf8mb4 ENGINE=InnoDB COLLATE utf8mb4_0900_ai_ci;

CREATE INDEX ix_conversations_owner_updated_id ON conversations (owner_account_id, updated_at, id);

CREATE TABLE knowledge_submissions (
	id VARCHAR(36) NOT NULL,
	submitter_account_id VARCHAR(36) NOT NULL,
	reviewer_account_id VARCHAR(36),
	filename VARCHAR(255) NOT NULL,
	object_key VARCHAR(512) NOT NULL,
	content_sha256 VARCHAR(64) NOT NULL,
	size_bytes INTEGER NOT NULL,
	page_count INTEGER NOT NULL,
	status ENUM('PENDING_REVIEW','PUBLISHING','PUBLISHED','PUBLISH_FAILED','REJECTED') NOT NULL,
	rejection_reason VARCHAR(500),
	processing_stage ENUM('PARSING','CHUNKING','EMBEDDING','INDEXING'),
	processing_progress INTEGER,
	processing_error_code VARCHAR(64),
	active_content_sha256 VARCHAR(64),
	created_at DATETIME(6) NOT NULL,
	reviewed_at DATETIME(6),
	published_at DATETIME(6),
	enabled BOOL NOT NULL DEFAULT 1,
	disabled_at DATETIME(6),
	es_cleanup_status ENUM('PENDING','RUNNING','SUCCEEDED','FAILED'),
	es_cleanup_error_code VARCHAR(64),
	es_cleanup_failed_at DATETIME(6),
	minio_cleanup_status ENUM('PENDING','RUNNING','SUCCEEDED','FAILED'),
	minio_cleanup_error_code VARCHAR(64),
	minio_cleanup_failed_at DATETIME(6),
	deleted_at DATETIME(6),
	PRIMARY KEY (id),
	FOREIGN KEY(reviewer_account_id) REFERENCES accounts (id),
	FOREIGN KEY(submitter_account_id) REFERENCES accounts (id),
	CONSTRAINT uq_submission_active_content_sha UNIQUE (active_content_sha256)
)CHARSET=utf8mb4 ENGINE=InnoDB COLLATE utf8mb4_0900_ai_ci;

CREATE INDEX ix_submission_owner_created ON knowledge_submissions (submitter_account_id, created_at, id);

CREATE TABLE conversation_attachments (
	id VARCHAR(36) NOT NULL,
	conversation_id VARCHAR(37) NOT NULL,
	owner_account_id VARCHAR(36) NOT NULL,
	filename VARCHAR(255) NOT NULL,
	size_bytes INTEGER NOT NULL,
	page_count INTEGER NOT NULL,
	content_sha256 VARCHAR(64) NOT NULL,
	object_key VARCHAR(512) NOT NULL,
	status ENUM('QUEUED','PROCESSING','READY','FAILED') NOT NULL,
	processing_stage ENUM('PARSING','CHUNKING','EMBEDDING','INDEXING'),
	processing_progress INTEGER,
	processing_error_code VARCHAR(64),
	queued_at DATETIME(6) NOT NULL,
	processing_started_at DATETIME(6),
	processed_at DATETIME(6),
	created_at DATETIME(6) NOT NULL,
	updated_at DATETIME(6) NOT NULL,
	deleted_at DATETIME(6),
	es_cleanup_status ENUM('PENDING','RUNNING','SUCCEEDED','FAILED'),
	es_cleanup_error_code VARCHAR(64),
	es_cleanup_failed_at DATETIME(6),
	minio_cleanup_status ENUM('PENDING','RUNNING','SUCCEEDED','FAILED'),
	minio_cleanup_error_code VARCHAR(64),
	minio_cleanup_failed_at DATETIME(6),
	PRIMARY KEY (id),
	FOREIGN KEY(conversation_id) REFERENCES conversations (id),
	FOREIGN KEY(owner_account_id) REFERENCES accounts (id),
	CONSTRAINT uq_attachment_conversation_sha UNIQUE (conversation_id, content_sha256)
)CHARSET=utf8mb4 ENGINE=InnoDB COLLATE utf8mb4_0900_ai_ci;

CREATE INDEX ix_attachment_conversation ON conversation_attachments (conversation_id, created_at, id);

CREATE INDEX ix_attachment_queue ON conversation_attachments (status, created_at, id);

CREATE TABLE messages (
	id VARCHAR(36) NOT NULL,
	conversation_id VARCHAR(37) NOT NULL,
	`role` ENUM('USER','ASSISTANT') NOT NULL,
	content TEXT NOT NULL,
	status ENUM('PENDING','COMPLETE','PARTIAL') NOT NULL,
	citations_json JSON NOT NULL,
	created_at DATETIME(6) NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(conversation_id) REFERENCES conversations (id)
)CHARSET=utf8mb4 ENGINE=InnoDB COLLATE utf8mb4_0900_ai_ci;

CREATE TABLE agent_runs (
	id VARCHAR(36) NOT NULL,
	actor_account_id VARCHAR(36) NOT NULL,
	conversation_id VARCHAR(37) NOT NULL,
	user_message_id VARCHAR(36) NOT NULL,
	assistant_message_id VARCHAR(36) NOT NULL,
	client_request_id VARCHAR(255) NOT NULL,
	status ENUM('RUNNING','SUCCEEDED','FAILED','PARTIAL') NOT NULL,
	route ENUM('GENERAL','RETRIEVAL'),
	source_scope_json JSON NOT NULL,
	trace_version VARCHAR(64),
	policy_version VARCHAR(64),
	trace_json JSON NOT NULL,
	error_code VARCHAR(64),
	started_at DATETIME(6) NOT NULL,
	finished_at DATETIME(6),
	PRIMARY KEY (id),
	FOREIGN KEY(actor_account_id) REFERENCES accounts (id),
	FOREIGN KEY(assistant_message_id) REFERENCES messages (id),
	FOREIGN KEY(conversation_id) REFERENCES conversations (id),
	FOREIGN KEY(user_message_id) REFERENCES messages (id)
)CHARSET=utf8mb4 ENGINE=InnoDB COLLATE utf8mb4_0900_ai_ci;

CREATE INDEX ix_agent_runs_actor_conversation_request ON agent_runs (actor_account_id, conversation_id, client_request_id);

CREATE INDEX ix_agent_runs_actor_status ON agent_runs (actor_account_id, status);

CREATE INDEX ix_agent_runs_conversation_status ON agent_runs (conversation_id, status);

CREATE TABLE agent_run_stages (
	id VARCHAR(36) NOT NULL,
	run_id VARCHAR(36) NOT NULL,
	stage ENUM('UNDERSTAND','PLAN','RETRIEVE_1','VERIFY','RETRIEVE_2','GENERATE') NOT NULL,
	status ENUM('RUNNING','SUCCEEDED','FAILED','INTERRUPTED') NOT NULL,
	started_at DATETIME(6) NOT NULL,
	finished_at DATETIME(6),
	summary_json JSON NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(run_id) REFERENCES agent_runs (id)
)CHARSET=utf8mb4 ENGINE=InnoDB COLLATE utf8mb4_0900_ai_ci;

INSERT INTO alembic_version (version_num) VALUES ('20260822_0009');
