from fastapi import FastAPI

from meyar.api.v1.router import api_router

app = FastAPI(title="MEYAR API", version="0.1.0")
app.include_router(api_router)
