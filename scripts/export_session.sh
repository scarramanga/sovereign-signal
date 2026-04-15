#!/bin/bash
# Exports the active LinkedIn session from the pod to ~/.sovereign-signal/linkedin_session.json
mkdir -p ~/.sovereign-signal
kubectl -n sovereign-signal exec deployment/sovereign-signal -- python3 -c "
import asyncio, json
from sqlalchemy import text
from server.database import AsyncSessionLocal

async def main():
    async with AsyncSessionLocal() as db:
        result = await db.execute(text(\"SELECT cookies, user_agent FROM ss_sessions WHERE platform = 'linkedin' AND valid = true ORDER BY created_at DESC LIMIT 1\"))
        row = result.fetchone()
    print(json.dumps({'cookies': json.loads(row[0]), 'user_agent': row[1]}))

asyncio.run(main())
" > ~/.sovereign-signal/linkedin_session.json
echo "Session exported to ~/.sovereign-signal/linkedin_session.json"
