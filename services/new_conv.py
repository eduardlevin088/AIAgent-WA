import asyncio
from openai import OpenAI
from config import GPT_KEY

client = OpenAI(api_key=GPT_KEY)


async def new_conversation(greeting: str | None = None):
    """Create an OpenAI conversation, optionally seeded with the greeting the
    bot already sent, so the model does not greet the customer a second time."""
    items = [{"type": "message", "role": "assistant", "content": greeting}] if greeting else []
    conversation = await asyncio.to_thread(client.conversations.create, items=items)
    return conversation.id
