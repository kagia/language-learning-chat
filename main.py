import chainlit as cl
from chainlit.input_widget import Select

from typing import cast, AnyStr

from dotenv import load_dotenv
import os

from autogen_agentchat.messages import TextMessage, ModelClientStreamingChunkEvent
from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.base import TaskResult
from autogen_core import CancellationToken
from autogen_ext.models.openai import OpenAIChatCompletionClient

from elevenlabs import stream
from elevenlabs.client import ElevenLabs

load_dotenv()

# For Finnish choose the largest model, fast models really struggle
# if using claude choose large for both for better instruction handling
MODEL_MAP = {"Dutch": "o4-mini-2025-04-16", "Finnish": "gpt-4.1-2025-04-14"}
VOICE_MAP = {"Dutch": "tvFp0BgJPrEXGoDhDIA4", "Finnish": "YSabzCJMvEHDduIDMdwV"}

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")


@cl.on_chat_start
async def on_chat_start():
    settings = await cl.ChatSettings(
        [
            Select(
                "lang",
                label="language",
                initial_index=0,
                values=["Dutch", "Finnish"],
                tooltip="Language to learn",
                description="Language to learn (Restart to take effect!)",
            ),
        ]
    ).send()

    initilize_assistant(settings)


def initilize_assistant(settings):
    model_client = OpenAIChatCompletionClient(
        model=MODEL_MAP[settings["lang"]],
        api_key=cast(str, OPENAI_API_KEY),
    )
    assistant = AssistantAgent(
        "assistant",
        model_client=model_client,
        model_client_stream=True,
        tools=[add_note],
        system_message=f"""
            You are a {settings["lang"]} language tutor. You assist the user by roleplaying a scenario. The style of language is casual.

            When correcting the user use English, When role playing speak {settings["lang"]}.

            Point out when the user's response sounds not natural or typical. Use the add_note tool to do this to keep the messages separate.
""",
    )
    cl.user_session.set("assistant", assistant)
    cl.user_session.set("model_client", model_client)


@cl.on_settings_update
async def setup_agent(settings):
    # should I just restart the app?
    initilize_assistant(settings)


@cl.step(name="translation", show_input=False, default_open=True)
async def translate(content):
    model_client = cast(OpenAIChatCompletionClient, cl.user_session.get("model_client"))
    settings = cl.context.session.chat_settings
    assistant = AssistantAgent(
        "translator",
        model_client=model_client,
        system_message=f"You translate the users {settings["lang"]} into english",
    )

    result = await assistant.run(task=f'"{content}"')

    last_message = result.messages[-1]
    if isinstance(last_message, TextMessage):
        return last_message.content

    return "ERROR: failed to translate"


async def vocalize(content):
    settings = cl.context.session.chat_settings
    client = ElevenLabs(api_key=ELEVENLABS_API_KEY)

    audio_stream = client.text_to_speech.stream(
        text=content,
        voice_id=VOICE_MAP[settings["lang"]],
        model_id="eleven_multilingual_v2",
    )

    # option 1: play the streamed audio locally
    stream(audio_stream)


async def add_note(content: str):
    """use this to add english suggestions or notes alongside responses"""
    cl.user_session.set("note", f"{content} \nTry again.")

    return "Note added"


@cl.action_callback("translate")
async def on_translate(action: cl.Action):
    await translate(action.payload["content"])


@cl.action_callback("vocalize")
async def on_vocalize(action: cl.Action):
    await vocalize(action.payload["content"])


@cl.set_starters
async def set_starters(_):
    return [
        cl.Starter(
            label="restaurant",
            message="Hey assistant you are a waiter and I a customer",
        ),
        cl.Starter(
            label="office",
            message="Hey assistant I want to practice an office scenario. You are my colleague and want help deploying a javascript application.",
        ),
        cl.Starter(
            label="family",
            message="Hey assistant I am your sisters new fiance, you are asking me questions about my self.",
        ),
    ]


@cl.on_stop
def on_stop():
    cancellation_token = cl.user_session.get("cancellation_token")
    if cancellation_token is not None:
        cancellation_token = cast(CancellationToken, cancellation_token)

        if cancellation_token.is_cancelled() is False:
            cancellation_token.cancel()


@cl.on_message
async def on_message(message: cl.Message) -> None:
    cancellation_token = CancellationToken()
    cl.user_session.set("cancellation_token", cancellation_token)

    assistant = cast(AssistantAgent, cl.user_session.get("assistant"))

    cl_msg = None

    async for chunk in assistant.run_stream(
        task=message.content, cancellation_token=cancellation_token
    ):
        if isinstance(chunk, ModelClientStreamingChunkEvent):
            if cl_msg is None:
                cl_msg = cl.Message(content=chunk.content)
            else:
                await cl_msg.stream_token(chunk.content)

        elif isinstance(chunk, TaskResult):
            note = cl.user_session.get("note")
            if cl_msg is not None:
                actions = [
                    cl.Action("translate", payload={"content": cl_msg.content}),
                    cl.Action("vocalize", payload={"content": cl_msg.content}),
                ]
                cl_msg.actions = actions

                if note is not None:
                    cl_msg.elements = [cl.Text(content=note)]
                    cl.user_session.set("note", None)

                await cl_msg.send()
                cl_msg = None
            elif note is not None:
                await cl.Message(note).send()
                cl.user_session.set("note", None)


@cl.password_auth_callback
async def auth_callback(username: str, password: str):
    if (username, password) == (
        ADMIN_USERNAME,
        ADMIN_PASSWORD,
    ):
        return cl.User(
            identifier="user",
            metadata={"role": "admin", "provider": "envfile"},
        )
    else:
        return None
