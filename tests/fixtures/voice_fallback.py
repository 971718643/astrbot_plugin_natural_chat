"""Bounded fallback for a rejected, atomic OneBot voice reply."""

import asyncio


def safe_to_fallback(error):
    if isinstance(error, (FileNotFoundError, PermissionError)):
        return True  # Serialization failed before any platform send.
    return (error.__class__.__name__ == "ActionFailed"
            and getattr(error, "retcode", None) not in (None, 0))


def wrap_voice_send(original, Record, Plain, MessageChain, ledger, logger):
    async def send(event, message):
        segments = message.chain
        records = [s for s in segments if isinstance(s, Record)]
        # Compound forwards/files can partly succeed; do not duplicate them.
        atomic = records and all(isinstance(s, (Record, Plain)) for s in segments)
        try:
            return await original(event, message)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not atomic or not safe_to_fallback(error):
                raise
            texts = []
            for segment in segments:
                text = getattr(segment, "text", None)
                if isinstance(text, str) and text.strip() and text.strip() not in texts:
                    texts.append(text.strip())
            if not texts:
                ledger.metric("voice_fallback_missing_text")
                raise
            logger.warning("[RuntimeGuard] Voice rejected; trying its existing text once (no model call)")
            fallback = MessageChain([Plain("\n".join(texts))])
            try:
                result = await original(event, fallback)
            except Exception:
                ledger.metric("voice_fallback_failed")
                logger.warning("[RuntimeGuard] Text fallback also failed; stopped without further retries")
                raise
            ledger.metric("voice_fallback_succeeded")
            logger.info("[RuntimeGuard] Voice reply delivered as text")
            return result
    return send
