"""M01 application configuration."""

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration required to construct the M01 application."""

    model_config = SettingsConfigDict(
        env_prefix="KNOWFLOW_",
        extra="ignore",
    )

    mysql_url: str
    jwt_secret: SecretStr
    seed_employee_username: str
    seed_employee_password: SecretStr
    seed_employee_display_name: str
    seed_admin_username: str
    seed_admin_password: SecretStr
    seed_admin_display_name: str
    chat_base_url: str | None = None
    chat_api_key: SecretStr | None = None
    chat_model: str | None = None
    run_deadline_seconds: int = 600
    elasticsearch_url: str = "http://127.0.0.1:9200"
    elasticsearch_index: str = "knowflow-documents"
    minio_endpoint: str = "http://127.0.0.1:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: SecretStr = SecretStr("minioadmin")
    minio_bucket: str = "knowflow-documents"
    embedding_base_url: str | None = None
    embedding_api_key: SecretStr | None = None
    embedding_model: str | None = None
    embedding_dimensions: int = 1024
    attachment_max_bytes: int = 10_485_760
    document_max_pages: int = 100
    docling_hf_home: str | None = None

    @field_validator(
        "mysql_url",
        "seed_employee_username",
        "seed_employee_display_name",
        "seed_admin_username",
        "seed_admin_display_name",
        mode="before",
    )
    @classmethod
    def require_text(cls, value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("value must not be empty")
        return value.strip()

    @field_validator("jwt_secret", "seed_employee_password", "seed_admin_password", mode="before")
    @classmethod
    def require_secret(cls, value: object) -> object:
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        if not isinstance(value, str) or not value.strip():
            raise ValueError("secret must not be empty")
        return value

    @field_validator(
        "chat_base_url", "chat_model", "embedding_base_url", "embedding_model", mode="before"
    )
    @classmethod
    def normalize_optional_text(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            return None
        return value.strip()

    @field_validator("chat_api_key", "embedding_api_key", mode="before")
    @classmethod
    def normalize_optional_secret(cls, value: object) -> object:
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            return None
        return value.strip()

    @field_validator("run_deadline_seconds")
    @classmethod
    def require_positive_run_deadline(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("run deadline must be positive")
        return value
