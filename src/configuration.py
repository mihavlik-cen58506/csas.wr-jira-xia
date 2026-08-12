import logging

from keboola.component.exceptions import UserException
from pydantic import BaseModel, Field, ValidationError, field_validator


class Configuration(BaseModel):
    """The 3 required Jira connection parameters, validated on load."""

    jira_base_url: str = Field(alias="JIRA_BASE_URL")
    jira_api_token: str = Field(alias="#jira_api_token")
    debug: bool = False

    def __init__(self, **data):
        try:
            super().__init__(**data)
        except ValidationError as e:
            error_messages = [f"{err['loc'][0]}: {err['msg']}" for err in e.errors()]
            raise UserException(f"Validation Error: {', '.join(error_messages)}")

        if self.debug:
            logging.debug("Component will run in Debug mode")

    @field_validator("jira_base_url")
    def base_url_must_be_https(cls, v):
        if not v.startswith("https://"):
            raise UserException("JIRA_BASE_URL must start with https://")
        return v.rstrip("/")
