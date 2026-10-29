"""Load the local .env without printing credentials and run the bot."""
import asyncio
import logging
import os
from pathlib import Path

for line in Path('.env').read_text().splitlines():
    if line.strip() and not line.lstrip().startswith('#'):
        key, value = line.split('=', 1)
        os.environ[key.strip()] = value.strip()

from vpn_bot.app import main
logging.basicConfig(level=logging.INFO)
try:
    asyncio.run(main())
except KeyboardInterrupt:
    pass
except Exception as exc:
    # Network exception messages may include the token-bearing Telegram URL.
    logging.error('Startup stopped: %s', type(exc).__name__)
    raise SystemExit(1)
