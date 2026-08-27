"""Entry point: `python main.py` then open http://localhost:8000"""
import uvicorn

from config import config

if __name__ == "__main__":
    uvicorn.run("server:app", host="127.0.0.1", port=config.PORT, log_level="info")
