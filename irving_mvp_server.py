import os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI()


class PromptRequest(BaseModel):
    """Model for the /run request body."""
    prompt: str


@app.get("/health")
async def health() -> dict:
    """Simple health check endpoint.

    Returns a JSON object indicating the service is up.
    """
    return {"status": "ok"}


@app.post("/run")
async def run(request: PromptRequest) -> dict:
    """Run a task using the provided prompt.

    This minimal implementation simply echoes the prompt back to the client.
    If you provide an Anthropic or OpenAI API key via environment variables, you can
    extend this function to call those APIs.
    """
    prompt = request.prompt.strip()

    # Determine which provider key is available (if any)
    api_key = (
        os.getenv("ANTHROPIC_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or os.getenv("GEMINI_API_KEY")
        or os.getenv("PERPLEXITY_API_KEY")
    )

    if not api_key:
        # In a production implementation, you would likely return an error or log
        # that no model key is configured. For this MVP we simply continue.
        pass

    # Echo the prompt back to the user. Modify here to integrate your model.
    return {"response": f"Received prompt: {prompt}"}
