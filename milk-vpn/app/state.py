class _State:
    bot = None  # aiogram.Bot, если бот запущен
    bot_username: str = ""


state = _State()
