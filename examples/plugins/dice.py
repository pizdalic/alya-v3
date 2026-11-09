"""Пример плагина: /dice [N] — бросить кубик, /dice_stats — сколько раз бросали.

Установка: скопируй в каталог plugins/ и выдай разрешения в config.toml:

    [plugins]
    enabled = true
    [plugins.grants]
    dice = ["reply", "storage"]
"""

import random

MANIFEST = {
    "name": "dice",
    "version": "1.0",
    "description": "бросить кубик",
    "commands": ["/dice", "/dice_stats"],
    "permissions": ["reply", "storage"],
    "audience": "all",
}


async def handle(ctx, command, args):  # type: ignore[no-untyped-def]
    if command == "/dice_stats":
        await ctx.reply(f"бросков: {await ctx.get('rolls', 0)}")
        return
    try:
        sides = max(2, min(int(args or 6), 1000))
    except ValueError:
        sides = 6
    await ctx.set("rolls", int(await ctx.get("rolls", 0)) + 1)
    await ctx.reply(f"выпало {random.randint(1, sides)} 🎲")
