"""Конфигурация: TOML-файл + переменные окружения ALYA_<СЕКЦИЯ>__<КЛЮЧ>.

Секреты (api_hash, api_key, bot_token) принимаются ТОЛЬКО из окружения,
чтобы они не попадали в файлы, которые легко случайно закоммитить.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (BaseModel, ConfigDict, Field, SecretStr, ValidationError,
                      field_validator, model_validator)

from alya.core.errors import ConfigError

ENV_PREFIX = "ALYA_"
SECRET_FIELDS: frozenset[tuple[str, str]] = frozenset(
    {
        ("telegram", "api_hash"),
        ("telegram", "bot_token"),
        ("telegram", "database_encryption_key"),
        ("llm", "api_key"),
    }
)
MODEL_ROLES: tuple[str, ...] = ("chat", "utility", "embedding", "vision", "stt", "tts", "image")
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AppSection(_Section):
    timezone: str = "Asia/Yekaterinburg"
    language: Literal["ru"] = "ru"
    data_dir: Path = Path("runtime")
    owner_user_id: int | None = Field(default=None, gt=0)

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"неизвестная таймзона: {value}") from exc
        return value


class TelegramSection(_Section):
    mode: Literal["userbot", "bot", "none"] = "userbot"
    api_id: int | None = Field(default=None, gt=0)
    api_hash: SecretStr | None = None
    bot_token: SecretStr | None = None
    session_dir: Path = Path("runtime/td_database")
    database_encryption_key: SecretStr | None = None
    alya_names: tuple[str, ...] = ("аля", "алька", "алечка", "алюша", "алюшка")
    debounce_s: float = Field(default=2.0, ge=0, le=10)
    debounce_max_wait_s: float = Field(default=8.0, ge=0.5, le=60)
    thread_ttl_s: float = Field(default=300.0, ge=30, le=3600)
    group_context_hours: int = Field(default=48, ge=24, le=72)
    private_history_days: int = Field(default=30, ge=1, le=365)
    strangers: Literal["known", "all", "owner_only"] = "known"
    gray_zone_llm: bool = True
    replies_per_10min: int = Field(default=20, ge=1, le=500)
    chat_send_interval_s: float = Field(default=1.0, ge=0, le=60)
    typing_chars_per_s: float = Field(default=12.0, gt=0, le=200)
    max_typing_delay_s: float = Field(default=6.0, ge=0, le=60)


class ModelRole(_Section):
    model: str = Field(min_length=1)
    base_url: str | None = None


class LLMSection(_Section):
    base_url: str = "https://openrouter.ai/api/v1"
    api_key: SecretStr | None = None
    timeout_s: float = Field(default=60.0, gt=0, le=600)
    roles: dict[str, ModelRole] = Field(default_factory=dict)

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        _validate_url(value)
        return value.rstrip("/")

    @field_validator("roles")
    @classmethod
    def _check_roles(cls, value: dict[str, ModelRole]) -> dict[str, ModelRole]:
        unknown = sorted(set(value) - set(MODEL_ROLES))
        if unknown:
            raise ValueError(f"неизвестные роли моделей: {', '.join(unknown)}")
        for role in value.values():
            if role.base_url is not None:
                _validate_url(role.base_url)
        return value


class StorageSection(_Section):
    db_path: Path = Path("runtime/alya.sqlite3")
    busy_timeout_ms: int = Field(default=5000, ge=0, le=60000)


class BackupSection(_Section):
    """Автоматические бэкапы (ADR-0010): хранятся retain_days дней, не больше keep штук."""

    enabled: bool = True
    dir: Path = Path("runtime/backups")
    interval_hours: float = Field(default=24.0, ge=1, le=168)
    keep: int = Field(default=7, ge=1, le=100)
    retain_days: int = Field(default=7, ge=1, le=90)


class HealthSection(_Section):
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)


class LoggingSection(_Section):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    format: Literal["json", "text"] = "json"


class MemorySection(_Section):
    mirror_enabled: bool = True
    mirror_dir: Path = Path("runtime/memory")
    working_ttl_hours: float = Field(default=6.0, gt=0, le=24 * 7)
    short_ttl_days: float = Field(default=14.0, gt=0, le=365)
    rrf_k: int = Field(default=60, ge=1, le=1000)
    search_candidates: int = Field(default=50, ge=1, le=1000)
    recency_half_life_days: float = Field(default=90.0, gt=0)
    vector_backend: Literal["numpy", "sqlite_vec"] = "numpy"
    embedding_batch: int = Field(default=32, ge=1, le=512)
    maintenance_interval_s: float = Field(default=300.0, ge=5)


class CharacterSection(_Section):
    dir: Path = Path("character")
    persona: str = Field(default="shy", pattern=r"^[a-z][a-z0-9_]{0,31}$")
    context_budget_tokens: int = Field(default=6000, ge=1000, le=200_000)
    recall_k: int = Field(default=8, ge=0, le=50)
    temperature: float = Field(default=0.9, ge=0, le=2)
    max_reply_tokens: int = Field(default=400, ge=32, le=4000)


class DreamSection(_Section):
    """Dream Engine: фоновая консолидация памяти (Phase 5)."""

    enabled: bool = True
    use_llm: bool = True
    hour_local: int = Field(default=4, ge=0, le=23)
    check_interval_s: float = Field(default=600.0, ge=10)
    new_memories_threshold: int = Field(default=200, ge=1)
    min_gap_hours: float = Field(default=6.0, ge=0, le=24 * 7)
    dup_threshold: float = Field(default=0.90, gt=0, le=1)
    exact_threshold: float = Field(default=0.97, gt=0, le=1)
    related_threshold: float = Field(default=0.60, gt=0, le=1)
    max_cluster: int = Field(default=8, ge=2, le=50)
    max_clusters_per_run: int = Field(default=50, ge=0, le=10_000)
    max_pairs_per_run: int = Field(default=40, ge=0, le=10_000)
    stale_days: float = Field(default=60.0, gt=0)
    decay_step: float = Field(default=0.1, ge=0, le=1)
    confidence_floor: float = Field(default=0.2, ge=0, le=1)
    contradiction_factor: float = Field(default=0.6, gt=0, le=1)
    retention_days: float = Field(default=30.0, ge=0)

    @model_validator(mode="after")
    def _check_thresholds(self) -> DreamSection:
        if not self.related_threshold < self.dup_threshold <= self.exact_threshold:
            raise ValueError("нужно related_threshold < dup_threshold <= exact_threshold")
        return self


_MEDIA_KINDS = ("vision", "stt", "tts", "image", "search", "reaction", "sticker")


class MediaSection(_Section):
    """Мультимодальность и инструменты (Phase 6). Модели — в llm.roles: vision/stt/tts/image."""

    vision: bool = True          # смотреть фото
    stt: bool = True             # слушать голосовые и кружки
    tts: bool = True             # иногда отвечать голосом
    images: bool = True          # «селфи» по просьбе
    stickers: bool = True
    reactions: bool = True
    web_search: bool = True
    stt_api: Literal["chat", "transcriptions"] = "chat"
    tts_voice: str = Field(default="nova", min_length=1, max_length=40)
    tts_instructions: str = Field(default="молодая девушка, тихо и нежно, по-русски",
                                  max_length=300)
    max_image_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, le=50 * 1024 * 1024)
    max_voice_seconds: int = Field(default=120, ge=5, le=600)
    max_voice_bytes: int = Field(default=20 * 1024 * 1024, ge=1024, le=50 * 1024 * 1024)
    media_dir: Path = Path("runtime/media")
    web_search_results: int = Field(default=3, ge=1, le=10)
    selfie_private_only: bool = True
    per_hour: dict[str, int] = Field(default_factory=lambda: {
        "vision": 30, "stt": 30, "tts": 10, "image": 3, "search": 10, "reaction": 60,
        "sticker": 30})
    per_day: dict[str, int] = Field(default_factory=lambda: {
        "vision": 200, "stt": 200, "tts": 40, "image": 10, "search": 60})

    @field_validator("per_hour", "per_day")
    @classmethod
    def _kinds(cls, value: dict[str, int]) -> dict[str, int]:
        unknown = sorted(set(value) - set(_MEDIA_KINDS))
        if unknown:
            raise ValueError(f"неизвестные виды лимитов: {', '.join(unknown)}")
        if any(v < 0 for v in value.values()):
            raise ValueError("лимит не может быть отрицательным")
        return value


class AutonomySection(_Section):
    """Автономия (Phase 7, ADR-0008): пишет первой, напоминает, читает каналы."""

    enabled: bool = True
    proactive_dm: bool = True
    group_initiative: bool = True
    read_channels: bool = True
    check_interval_s: float = Field(default=900.0, ge=10)
    quiet_start_hour: int = Field(default=23, ge=0, le=23)
    quiet_end_hour: int = Field(default=9, ge=0, le=23)
    dm_threshold: float = Field(default=0.55, ge=0, le=1)
    dm_min_silence_hours: float = Field(default=8.0, ge=0)
    dm_max_silence_days: float = Field(default=14.0, gt=0)
    dm_min_interactions: int = Field(default=20, ge=0)
    dm_per_person_hours: float = Field(default=24.0, ge=1)
    dm_max_per_day: int = Field(default=3, ge=0)
    consider_cooldown_hours: float = Field(default=3.0, ge=0)
    group_window_minutes: float = Field(default=20.0, gt=0)
    group_min_messages: int = Field(default=6, ge=1)
    group_min_participants: int = Field(default=3, ge=2)
    group_min_hits: int = Field(default=2, ge=1)
    group_cooldown_hours: float = Field(default=4.0, ge=0)
    group_max_per_day: int = Field(default=4, ge=0)
    channel_digest_hours: float = Field(default=12.0, gt=0)
    channel_max_facts: int = Field(default=3, ge=0, le=10)
    jitter: float = Field(default=0.08, ge=0, le=0.5)


class ModerationSection(_Section):
    """Модерация в группах: только запись в audit_log, без действий (T11)."""

    enabled: bool = False
    dry_run: Literal[True] = True  # настоящих банов и удалений нет и не будет без ADR
    flood_messages: int = Field(default=8, ge=2, le=100)
    flood_window_s: int = Field(default=30, ge=5, le=3600)
    max_links: int = Field(default=3, ge=0, le=50)
    banned_words: list[str] = Field(default_factory=list)


class PluginsSection(_Section):
    """Плагины (Phase 8): загружаются только перечисленные в grants."""

    enabled: bool = False
    dir: Path = Path("plugins")
    timeout_s: float = Field(default=10.0, gt=0, le=120)
    grants: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("grants")
    @classmethod
    def _known_permissions(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        known = {"reply", "recall", "storage"}
        for name, perms in value.items():
            bad = sorted(set(perms) - known)
            if bad:
                raise ValueError(f"plugins.grants.{name}: неизвестные разрешения {bad}")
        return value


class Settings(_Section):
    app: AppSection = Field(default_factory=AppSection)
    telegram: TelegramSection = Field(default_factory=TelegramSection)
    llm: LLMSection = Field(default_factory=LLMSection)
    storage: StorageSection = Field(default_factory=StorageSection)
    health: HealthSection = Field(default_factory=HealthSection)
    logging: LoggingSection = Field(default_factory=LoggingSection)
    memory: MemorySection = Field(default_factory=MemorySection)
    character: CharacterSection = Field(default_factory=CharacterSection)
    dream: DreamSection = Field(default_factory=DreamSection)
    media: MediaSection = Field(default_factory=MediaSection)
    autonomy: AutonomySection = Field(default_factory=AutonomySection)
    plugins: PluginsSection = Field(default_factory=PluginsSection)
    moderation: ModerationSection = Field(default_factory=ModerationSection)
    backup: BackupSection = Field(default_factory=BackupSection)

    def problems_for_run(self) -> list[str]:
        """Чего не хватает для полноценной работы (формально конфиг при этом валиден)."""
        problems: list[str] = []
        tg = self.telegram
        if tg.mode == "userbot":
            if tg.api_id is None:
                problems.append("telegram.api_id не задан (ALYA_TELEGRAM__API_ID)")
            if tg.api_hash is None:
                problems.append("telegram.api_hash не задан (ALYA_TELEGRAM__API_HASH)")
        if tg.mode == "bot" and tg.bot_token is None:
            problems.append("telegram.bot_token не задан (ALYA_TELEGRAM__BOT_TOKEN)")
        if self.llm.api_key is None:
            problems.append("llm.api_key не задан (ALYA_LLM__API_KEY)")
        if "chat" not in self.llm.roles:
            problems.append("llm.roles.chat не задана: нужна основная модель диалога")
        if self.app.owner_user_id is None:
            problems.append("app.owner_user_id не задан: команды владельца будут недоступны")
        return problems

    def secret_values(self) -> list[str]:
        """Значения секретов — для фильтра логов."""
        values = [self.telegram.api_hash, self.telegram.bot_token,
                  self.telegram.database_encryption_key, self.llm.api_key]
        return [v.get_secret_value() for v in values if v is not None]

    def summary(self) -> dict[str, str]:
        """Безопасная для вывода сводка: вместо секретов — «задан/не задан»."""

        def mark(value: SecretStr | None) -> str:
            return "задан" if value is not None else "не задан"

        roles = ", ".join(f"{k}={v.model}" for k, v in sorted(self.llm.roles.items())) or "—"
        return {
            "app.timezone": self.app.timezone,
            "app.data_dir": str(self.app.data_dir),
            "telegram.mode": self.telegram.mode,
            "telegram.api_hash": mark(self.telegram.api_hash),
            "llm.base_url": self.llm.base_url,
            "llm.api_key": mark(self.llm.api_key),
            "llm.roles": roles,
            "storage.db_path": str(self.storage.db_path),
            "character.persona": self.character.persona,
            "health": f"{self.health.host}:{self.health.port}" if self.health.enabled else "off",
        }


def _validate_url(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme == "https" and parsed.netloc:
        return
    if parsed.scheme == "http" and parsed.hostname in _LOCAL_HOSTS:
        return
    raise ValueError("URL должен быть https:// (http:// допустим только для localhost)")


def _env_overrides(env: Mapping[str, str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in env.items():
        if not key.startswith(ENV_PREFIX) or "__" not in key:
            continue
        parts = [p.lower() for p in key[len(ENV_PREFIX) :].split("__")]
        if any(not p for p in parts):
            raise ConfigError(f"некорректное имя переменной окружения: {key}")
        node = result
        for part in parts[:-1]:
            child = node.setdefault(part, {})
            if not isinstance(child, dict):
                raise ConfigError(f"конфликт переменных окружения для {key}")
            node = child
        node[parts[-1]] = value
    return result


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _format_validation_error(exc: ValidationError) -> str:
    # Намеренно без input_value: в нём может оказаться секрет.
    lines = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        lines.append(f"{loc}: {err['msg']}")
    return "; ".join(lines)


def load_settings(path: Path | None = None, env: Mapping[str, str] | None = None) -> Settings:
    """Загрузить настройки. Приоритет: окружение > файл > значения по умолчанию."""
    environ = os.environ if env is None else env
    data: dict[str, Any] = {}
    if path is not None:
        try:
            with path.open("rb") as fh:
                data = tomllib.load(fh)
        except FileNotFoundError as exc:
            raise ConfigError(f"файл конфигурации не найден: {path}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"ошибка синтаксиса TOML в {path}: {exc}") from exc
        for section, key in sorted(SECRET_FIELDS):
            block = data.get(section)
            if isinstance(block, dict) and key in block:
                env_name = f"{ENV_PREFIX}{section.upper()}__{key.upper()}"
                raise ConfigError(
                    f"{section}.{key} — секрет, его нельзя хранить в {path.name}; "
                    f"задайте его через {env_name} в .env"
                )
    merged = _deep_merge(data, _env_overrides(environ))
    try:
        return Settings.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc)) from None
