"""Авторизация аккаунта в TDLib.

Интерактивный вход — только командой alya login: код и пароль 2FA вводятся
вручную и нигде не сохраняются. Обычный запуск без сессии останавливается
с подсказкой (AuthRequired), а не ждёт ввода.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from alya import __version__
from alya.core.config import Settings
from alya.core.errors import AlyaError, ConfigError
from alya.telegram.tdlib.client import TdClient, TdError, TdObject
from alya.telegram.tdlib.tdjson import TdJson, load_tdjson
from alya.telegram.transport import TransportError

type Prompt = Callable[[str, str], Awaitable[str]]
type Notify = Callable[[str], None]

LOGIN_HINT = "нужен вход в Telegram: выполните «docker compose run --rm -it alya login»"
_MAX_TRIES = 5


class AuthRequired(AlyaError):
    pass


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)


def tdlib_parameters(settings: Settings) -> dict[str, Any]:
    tg = settings.telegram
    if tg.api_id is None or tg.api_hash is None:
        raise ConfigError("для userbot нужны ALYA_TELEGRAM__API_ID и ALYA_TELEGRAM__API_HASH")
    root = Path(tg.session_dir)
    _private_dir(root)
    key = tg.database_encryption_key.get_secret_value() if tg.database_encryption_key else ""
    return {
        "use_test_dc": False,
        "database_directory": str(root / "db"),
        "files_directory": str(root / "files"),
        "database_encryption_key": base64.b64encode(key.encode("utf-8")).decode("ascii"),
        "use_file_database": False,
        "use_chat_info_database": True,
        "use_message_database": True,
        "use_secret_chats": False,
        "api_id": tg.api_id,
        "api_hash": tg.api_hash.get_secret_value(),
        "system_language_code": "ru",
        "device_model": "Alya",
        "system_version": "Linux",
        "application_version": __version__,
    }


async def _ask_until_ok(client: TdClient, prompt: Prompt, notify: Notify, kind: str, hint: str,
                        build: Callable[[str], TdObject]) -> None:
    for attempt in range(_MAX_TRIES):
        value = (await prompt(kind, hint)).strip()
        if not value:
            continue
        try:
            await client.request(build(value))
            return
        except TdError as exc:
            if exc.code != 400 or attempt == _MAX_TRIES - 1:
                raise AuthRequired(f"вход не удался: {exc.td_message}") from None
            notify(f"не подошло ({exc.td_message}), попробуйте ещё раз")
    raise AuthRequired("вход отменён")


async def authorize(client: TdClient, settings: Settings, prompt: Prompt | None, *,
                    notify: Notify = lambda _msg: None, timeout_s: float = 600.0) -> TdObject:
    """Довести клиента до authorizationStateReady и вернуть getMe."""
    params = tdlib_parameters(settings)
    states: asyncio.Queue[TdObject] = asyncio.Queue()

    async def watch(update: TdObject) -> None:
        if update.get("@type") == "updateAuthorizationState":
            states.put_nowait(update.get("authorization_state") or {})

    client.on_update(watch)
    try:
        await client.start()
        with contextlib.suppress(TransportError):
            await client.request({"@type": "getOption", "name": "version"}, timeout=30)

        def need_prompt() -> Prompt:
            if prompt is None:
                raise AuthRequired(LOGIN_HINT)
            return prompt

        while True:
            try:
                state = await asyncio.wait_for(states.get(), timeout_s)
            except TimeoutError:
                raise AuthRequired("TDLib не ответил при входе") from None
            kind = state.get("@type", "")
            if kind == "authorizationStateWaitTdlibParameters":
                await client.request({"@type": "setTdlibParameters", **params})
            elif kind == "authorizationStateWaitPhoneNumber":
                await _ask_until_ok(client, need_prompt(), notify, "phone",
                                    "номер телефона в формате +7…",
                                    lambda v: {"@type": "setAuthenticationPhoneNumber",
                                               "phone_number": v})
            elif kind == "authorizationStateWaitCode":
                await _ask_until_ok(client, need_prompt(), notify, "code", "код из Telegram",
                                    lambda v: {"@type": "checkAuthenticationCode", "code": v})
            elif kind == "authorizationStateWaitPassword":
                hint = state.get("password_hint") or ""
                await _ask_until_ok(client, need_prompt(), notify, "password",
                                    "пароль 2FA" + (f" (подсказка: {hint})" if hint else ""),
                                    lambda v: {"@type": "checkAuthenticationPassword",
                                               "password": v})
            elif kind == "authorizationStateWaitEmailAddress":
                await _ask_until_ok(client, need_prompt(), notify, "email", "почта для входа",
                                    lambda v: {"@type": "setAuthenticationEmailAddress",
                                               "email_address": v})
            elif kind == "authorizationStateWaitEmailCode":
                await _ask_until_ok(client, need_prompt(), notify, "email_code", "код из письма",
                                    lambda v: {"@type": "checkAuthenticationEmailCode",
                                               "code": {"@type": "emailAddressAuthenticationCode",
                                                        "code": v}})
            elif kind == "authorizationStateReady":
                return await client.request({"@type": "getMe"})
            elif kind in ("authorizationStateWaitRegistration",
                          "authorizationStateWaitOtherDeviceConfirmation"):
                raise AuthRequired("аккаунт нужно сначала завести в официальном клиенте Telegram")
            elif kind in ("authorizationStateLoggingOut", "authorizationStateClosing",
                          "authorizationStateClosed"):
                raise AuthRequired("сессия Telegram закрыта; " + LOGIN_HINT)
    finally:
        client.remove_handler(watch)


def username_of(user: TdObject) -> str | None:
    names = (user.get("usernames") or {}).get("active_usernames") or []
    return names[0] if names else user.get("username") or None


async def interactive_login(settings: Settings, prompt: Prompt, *, notify: Notify = print,
                            loader: Callable[[], TdJson] = load_tdjson) -> TdObject:
    client = TdClient(loader())
    try:
        return await authorize(client, settings, prompt, notify=notify)
    finally:
        await client.close()
