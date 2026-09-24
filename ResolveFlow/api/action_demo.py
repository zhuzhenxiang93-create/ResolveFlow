"""Standalone demo service without Redis, Chroma or LLM API dependencies."""
from fastapi import FastAPI

from api.action_routes import router

app = FastAPI(title="ResolveFlow Agent execution demo")
app.include_router(router)

from api.commerce_routes import router as commerce_router
app.include_router(commerce_router)
