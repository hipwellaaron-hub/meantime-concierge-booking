from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    test_database_url: str = ""

    # Guided Booking Wizard auto-routing. Both default OFF: a clean wizard
    # submission (no [REVIEW] markers, no escalation) is technically ready
    # to generate/send automatically, but "nothing client-facing auto-
    # sends" is a project-wide rule -- these flags exist so Aaron can
    # decide to relax it deliberately, per document (BEO is internal/staff-
    # facing, the invoice is client-facing, so they're independently
    # switchable rather than one combined flag).
    wizard_beo_auto_finalize: bool = False
    wizard_invoice_auto_send: bool = False


settings = Settings()
