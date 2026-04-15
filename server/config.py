from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    database_url: str = ""
    resend_api_key: str = ""
    anthropic_api_key: str = ""
    linkedin_session_cookie: str = ""
    from_email: str = ""
    alert_email: str = ""
    ss_base_url: str = "http://localhost:8080"
    environment: str = "production"

    class Config:
        env_file = ".env"

settings = Settings()
