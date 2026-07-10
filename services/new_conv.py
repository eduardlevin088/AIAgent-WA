import asyncio
from openai import OpenAI
from config import GPT_KEY

client = OpenAI(api_key=GPT_KEY)


async def new_conversation():
    conversation = await asyncio.to_thread(client.conversations.create)
    return conversation.id