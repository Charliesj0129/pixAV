import asyncio
import os

import asyncpg


async def run():
    dsn = os.environ.get('PIXAV_DSN', 'postgresql://pixav:pixav@localhost:5432/pixav')
    conn = await asyncpg.connect(dsn)
    await conn.execute('ALTER TABLE accounts ADD COLUMN IF NOT EXISTS password TEXT')
    await conn.execute(
        '''INSERT INTO accounts (email, password, status) 
           VALUES ('ren0129b@gmail.com', 'Ren-4568357', 'active') 
           ON CONFLICT (email) DO UPDATE SET password = EXCLUDED.password, status = 'active'
        '''
    )
    print('DB schema updated and user registered!')
    await conn.close()

asyncio.run(run())
