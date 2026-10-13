from __future__ import annotations

import os
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_ai.models import Model
from pydantic_ai.models.groq import GroqModel
from pydantic_ai.models.ollama import OllamaModel
from pydantic_ai.providers.groq import GroqProvider
from pydantic_ai.providers.ollama import OllamaProvider
from pydantic_settings import BaseSettings, SettingsConfigDict


class ModelRole(str, Enum):
    orchestrator = "orchestrator"
    synthesis = "synthesis"
    sub_agent = "sub_agent"
    redteam = "redteam"
    chat = "chat"


class RoleConfig(BaseModel):
    provider: Literal["ollama", "groq"]
    model: str
    description: str = ""


class ModelConfig(BaseModel):
    roles: dict[str, RoleConfig] = Field(default_factory=dict)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ollama_base_url: str = "https://ollama.com/v1"
    ollama_api_key: str = ""
    ollama_cloud_api_key: str = ""  # support common .env name
    groq_api_key: str = ""
    logfire_token: str = ""
    gc_contract_path: str = "policies/food_service.v1.yaml"
    gc_engine_host: str = "0.0.0.0"
    gc_engine_port: int = 8080


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _parse_override(value: str) -> tuple[str, str]:
    if ":" not in value:
        raise ValueError(f"Model override must be provider:model, got {value!r}")
    provider, model = value.split(":", 1)
    if provider not in ("ollama", "groq"):
        raise ValueError(f"Unknown provider {provider!r}")
    return provider, model


@lru_cache
def load_model_config() -> ModelConfig:
    config_path = _project_root() / "config" / "models.yaml"
    if config_path.exists():
        data = yaml.safe_load(config_path.read_text()) or {}
        return ModelConfig.model_validate(data)

    return ModelConfig(
        roles={
            "orchestrator": RoleConfig(
                provider="groq",
                model="llama-3.3-70b-versatile",
                description="Synthesis orchestrator (research + draft)",
            ),
            "synthesis": RoleConfig(
                provider="groq",
                model="llama-3.3-70b-versatile",
                description="Policy drafting agent",
            ),
            "sub_agent": RoleConfig(
                provider="groq",
                model="llama-3.3-70b-versatile",
                description="Lightweight sub-tasks",
            ),
            "redteam": RoleConfig(
                provider="groq",
                model="llama-3.3-70b-versatile",
                description="Probe generation",
            ),
            "chat": RoleConfig(
                provider="groq",
                model="llama-3.3-70b-versatile",
                description="Demo chatbot",
            ),
        }
    )


def _role_override(role: ModelRole) -> tuple[str, str] | None:
    env_key = f"GC_MODEL_{role.value.upper()}"
    raw = os.environ.get(env_key)
    if raw:
        return _parse_override(raw)
    return None


def get_model(role: ModelRole | str) -> Model:
    if isinstance(role, str):
        role = ModelRole(role)

    settings = Settings()
    config = load_model_config()
    role_key = role.value
    role_cfg = config.roles.get(role_key)
    if role_cfg is None:
        raise KeyError(f"No model config for role {role_key!r}")

    override = _role_override(role)
    provider = override[0] if override else role_cfg.provider
    model_name = override[1] if override else role_cfg.model

    if provider == "ollama":
        # Support multiple common env var names from user's .env
        api_key = (
            settings.ollama_api_key
            or settings.ollama_cloud_api_key
            or os.environ.get("OLLAMA_API_KEY")
            or os.environ.get("OLLAMA_CLOUD_API_KEY")
        )

        base_url = settings.ollama_base_url or "https://ollama.com/v1"
        # Ensure we hit the OpenAI-compatible /v1 endpoint, not the marketing site
        if "ollama.com" in base_url and not base_url.rstrip("/").endswith("/v1"):
            base_url = base_url.rstrip("/") + "/v1"

        return OllamaModel(
            model_name,
            provider=OllamaProvider(
                base_url=base_url,
                api_key=api_key,
            ),
        )
    if provider == "groq":
        return GroqModel(
            model_name,
            provider=GroqProvider(api_key=settings.groq_api_key or os.environ.get("GROQ_API_KEY")),
        )
    raise ValueError(f"Unsupported provider {provider!r}")


def model_label(role: ModelRole | str) -> str:
    if isinstance(role, str):
        role = ModelRole(role)
    override = _role_override(role)
    if override:
        return f"{override[0]}:{override[1]}"
    cfg = load_model_config().roles[role.value]
    return f"{cfg.provider}:{cfg.model}"