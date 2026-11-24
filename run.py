"""Start Server Assistant: python run.py"""
import logging

import uvicorn

from app.config import load_config
from app.discovery import computer_name, lan_ip


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    cfg = load_config()
    port = cfg["server"]["port"]
    print(f"\n  Dashboard on this PC:   http://localhost:{port}")
    print(f"  Dashboard on your network: http://{lan_ip()}:{port}")
    print(f"  Or by name (survives IP changes): http://{computer_name()}:{port}  or  http://serverassistant.local:{port}\n")
    uvicorn.run("app.web:app", host=cfg["server"]["host"], port=port, log_level="warning")
