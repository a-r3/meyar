from fastapi import FastAPI

from meyar.api.v1.router import api_router
from meyar.ui.router import install_ui

app = FastAPI(title="MEYAR API", version="0.1.0")
app.include_router(api_router)
install_ui(app)
