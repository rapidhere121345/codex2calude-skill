"""FastMCP surface for the unified remote-research gateway."""
from __future__ import annotations

from typing import Literal

from mcp.server.fastmcp import FastMCP

from .gateway import Gateway, GatewayError, load_settings

INSTRUCTIONS = (
    "Run bounded remote research with an explicit provider and model. "
    "Never change a conversation's provider, model or effort on "
    "continuation. Do not expose native session IDs or credentials."
)

DESCRIPTIONS = {
    "research_status":
        "Return sanitized provider availability, model choices, and quota "
        "observations.",
    "research_once":
        "Run one isolated research request with the selected provider and "
        "model.",
    "research_start":
        "Start a research conversation with an immutable provider and model "
        "selection.",
    "research_continue":
        "Continue an owned conversation using its original provider and "
        "model.",
    "research_list_conversations":
        "List owned unified conversations for one provider.",
    "research_export_conversation":
        "Export readable content from one owned research conversation.",
    "research_close":
        "Close one owned conversation mapping without deleting its "
        "transcripts.",
}

Provider = Literal["claude", "gemini"]


def create_app(gateway: Gateway) -> FastMCP:
    app = FastMCP("remote_research", instructions=INSTRUCTIONS)

    @app.tool(description=DESCRIPTIONS["research_status"])
    async def research_status(
        provider: Provider | None = None,
    ) -> dict:
        return await gateway.status(provider)

    @app.tool(description=DESCRIPTIONS["research_once"])
    async def research_once(
        prompt: str,
        provider: Provider,
        model: str | None = None,
        use_web: bool = True,
        max_turns: int = 50,
        timeout_seconds: int = 900,
    ) -> dict:
        return await gateway.once(prompt, provider, model, use_web,
                                  max_turns, timeout_seconds)

    @app.tool(description=DESCRIPTIONS["research_start"])
    async def research_start(
        prompt: str,
        provider: Provider,
        model: str | None = None,
        title: str | None = None,
        use_web: bool = True,
        max_turns: int = 50,
        timeout_seconds: int = 900,
    ) -> dict:
        return await gateway.start(prompt, provider, model, title, use_web,
                                   max_turns, timeout_seconds)

    @app.tool(description=DESCRIPTIONS["research_continue"])
    async def research_continue(
        conversation_id: str,
        prompt: str,
        max_turns: int = 50,
        timeout_seconds: int = 900,
    ) -> dict:
        return await gateway.continue_conversation(
            conversation_id, prompt, max_turns, timeout_seconds)

    @app.tool(description=DESCRIPTIONS["research_list_conversations"])
    async def research_list_conversations(
        provider: Provider,
        include_closed: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        return await gateway.list_conversations(
            provider, include_closed, limit, offset)

    @app.tool(description=DESCRIPTIONS["research_export_conversation"])
    async def research_export_conversation(
        conversation_id: str, format: str = "markdown",
    ) -> dict:
        return await gateway.export_conversation(conversation_id, format)

    @app.tool(description=DESCRIPTIONS["research_close"])
    async def research_close(conversation_id: str) -> dict:
        return await gateway.close(conversation_id)

    return app


def main() -> None:
    try:
        settings = load_settings()
        gateway = Gateway(settings)
        app = create_app(gateway)
        app.run()
    except GatewayError as exc:
        raise SystemExit(f"remote_research: {exc}") from exc


if __name__ == "__main__":
    main()
