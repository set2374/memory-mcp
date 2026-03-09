"""Entry point for Memory MCP Server: python -m app"""

import asyncio

from app.server import main

if __name__ == "__main__":
    asyncio.run(main())
else:
    asyncio.run(main())
