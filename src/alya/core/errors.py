"""Исключения приложения."""


class AlyaError(Exception):
    """Базовое исключение проекта."""


class ConfigError(AlyaError):
    """Некорректная конфигурация. Текст не содержит значений секретов."""


class ContainerError(AlyaError):
    """Ошибка регистрации или разрешения зависимостей."""


class MigrationError(AlyaError):
    """Ошибка миграций БД."""


class StorageError(AlyaError):
    """Ошибка хранилища."""
