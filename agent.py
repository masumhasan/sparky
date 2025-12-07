from dotenv import load_dotenv

from livekit import agents
from livekit.agents import AgentSession, Agent, RoomInputOptions, ChatContext
from livekit.plugins import (
    noise_cancellation,
    openai
)
from livekit.plugins import google
from prompts import AGENT_INSTRUCTION, SESSION_INSTRUCTION
from tools import get_weather, search_web, send_email
from mem0 import AsyncMemoryClient
from mcp_client import MCPServerSse
from mcp_client.agent_tools import MCPToolsIntegration
import os
import json
import logging
load_dotenv()


class Assistant(Agent):
    def __init__(self, chat_ctx=None) -> None:
        super().__init__(
            instructions=AGENT_INSTRUCTION,
            llm="gemini-2.0-flash",
            # If your Agent accepts an LLM config, replace the line above with a dict or factory call, for example:
            #llm={"name": "gemini-2.0-flash", "voice": "Aoede", "temperature": 0.8},
            tools=[
            get_weather,
            search_web,
            send_email
            ],
            chat_ctx=chat_ctx
        )
        


async def entrypoint(ctx: agents.JobContext):

    async def shutdown_hook(chat_ctx: ChatContext, mem0: AsyncMemoryClient, memory_str: str, user_id: str = "Masum"):
        logging.info("Shutting down, saving chat context to memory...")

        messages_formatted = [
        ]

        logging.info(f"Chat context messages: {chat_ctx.items}")

        for item in chat_ctx.items:
            content_str = ''.join(item.content) if isinstance(item.content, list) else str(item.content)

            if memory_str and memory_str in content_str:
                continue

            if item.role in ['user', 'assistant']:
                messages_formatted.append({
                    "role": item.role,
                    "content": content_str.strip()
                })

        logging.info(f"Formatted messages to add to memory: {messages_formatted}")
        await mem0.add(messages_formatted, user_id=user_id)
        logging.info("Chat context saved to memory.")


    session = AgentSession(
        
    )

    

    mem0 = AsyncMemoryClient()

    # Determine user name: prefer environment variable, then prompt the operator
    user_name = os.environ.get("SPARKY_USER")
    if not user_name:
        try:
            # Prompt in the running process so the operator can provide the user's name
            print("Please enter the user's name to use for memory storage (leave blank to use 'New_User'):")
            entered = input().strip()
            user_name = entered if entered else 'New_User'
        except Exception:
            # Non-interactive environment fallback
            user_name = 'New_User'

    results = await mem0.get_all(user_id=user_name)
    initial_ctx = ChatContext()
    memory_str = ''

    if results:
        memories = [
            {
                "memory": result["memory"],
                "updated_at": result["updated_at"]
            }
            for result in results
        ]
        memory_str = json.dumps(memories)
        logging.info(f"Memories: {memory_str}")
        initial_ctx.add_message(
            role="assistant",
            content=f"The user's name is {user_name}, and this is relvant context about him: {memory_str}."
        )

    mcp_server = MCPServerSse(
        params={"url": os.environ.get("N8N_MCP_SERVER_URL")},
        cache_tools_list=True,
        name="SSE MCP Server"
    )

    agent = await MCPToolsIntegration.create_agent_with_tools(
        agent_class=Assistant, agent_kwargs={"chat_ctx": initial_ctx},
        mcp_servers=[mcp_server]
    )

    await session.start(
        room=ctx.room,
        agent=agent,
        room_input_options=RoomInputOptions(
            # LiveKit Cloud enhanced noise cancellation
            # - If self-hosting, omit this parameter
            # - For telephony applications, use `BVCTelephony` for best results
            video_enabled=True,
            noise_cancellation=noise_cancellation.BVC(),
        ),
    )

    await ctx.connect()

    try:
        await session.generate_reply(
            instructions=SESSION_INSTRUCTION,
        )
    except AssertionError as e:
        # livekit may raise an AssertionError when no TTS node is available; log and continue
        logging.warning(f"generate_reply skipped due to assertion: {e}")
    except Exception as e:
        # Log other unexpected exceptions but don't let the worker crash here
        logging.exception("Unexpected error during session.generate_reply")

    # Ensure the shutdown hook uses the resolved user_name
    ctx.add_shutdown_callback(lambda: shutdown_hook(session._agent.chat_ctx, mem0, memory_str, user_name))

if __name__ == "__main__":
    agents.cli.run_app(agents.WorkerOptions(entrypoint_fnc=entrypoint))