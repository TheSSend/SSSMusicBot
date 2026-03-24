import time
import asyncio
import logging

logger = logging.getLogger(__name__)

# последний edit по каналу
_last_channel_edit = {}

# блокировки по каналу
_channel_locks = {}

RATE_LIMIT_SECONDS = 2

# Очистка старых записей каждые час
_cleanup_task = None


def start_cleanup_task():
    """Start periodic cleanup of old entries"""
    global _cleanup_task
    if _cleanup_task is None or _cleanup_task.done():
        _cleanup_task = asyncio.create_task(_periodic_cleanup())


async def _periodic_cleanup():
    """Periodically clean old entries to prevent memory leaks"""
    while True:
        await asyncio.sleep(3600)  # Every hour
        now = time.time()
        # Remove entries older than 1 day
        cutoff = now - 86400
        to_delete = [
            channel_id for channel_id, last_edit in _last_channel_edit.items()
            if last_edit < cutoff
        ]
        for channel_id in to_delete:
            _last_channel_edit.pop(channel_id, None)
            _channel_locks.pop(channel_id, None)
        logger.debug(f"Cleaned up {len(to_delete)} old channel entries")


async def safe_message_edit(message, **kwargs) -> None:

    if not message:
        return

    channel_id = message.channel.id
    now = time.time()

    # создаём lock если нет
    if channel_id not in _channel_locks:
        _channel_locks[channel_id] = asyncio.Lock()

    async with _channel_locks[channel_id]:

        last = _last_channel_edit.get(channel_id, 0)

        # если прошло мало времени — ждём
        diff = now - last
        if diff < RATE_LIMIT_SECONDS:
            await asyncio.sleep(RATE_LIMIT_SECONDS - diff)

        try:
            await message.edit(**kwargs)
            _last_channel_edit[channel_id] = time.time()
        except Exception as e:
            logger.error(f"Failed to edit message: {e}")
            # Don't silently fail - at least log the error