from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    hermes_db_path: str = "/home/cflux/.hermes/state.db"
    ego_db_path: str = "/mnt/LargeStorage/AgentEgo/data/ego.db"
    retention_days: int = 7
    host: str = "0.0.0.0"
    port: int = 8765
    log_level: str = "info"
    display_timezone: str = "America/Los_Angeles"

    model_config = {"env_prefix": "EGO_", "env_file": ".env"}


settings = Settings()
