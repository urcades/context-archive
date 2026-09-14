import asyncio
import json
import os
from pathlib import Path
import sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from context_archive import core, cli


def test_stdio_readonly_parity(tmp_path):
    archive=tmp_path/'archive.sqlite3'; core.initialize(archive)
    async def run():
        params=StdioServerParameters(command=sys.executable,args=['-m','context_archive.mcpserver','--archive',str(archive)],env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src')})
        async with stdio_client(params) as (read,write):
            async with ClientSession(read,write) as session:
                await session.initialize()
                listing=await session.list_tools()
                assert {x.name for x in listing.tools}==set(cli.READ_TOOLS)
                assert all('archive' not in x.inputSchema['properties'] for x in listing.tools)
                result=await session.call_tool('sources',{})
                assert not result.isError
                assert json.loads(result.content[0].text)==cli.invoke('sources',archive,{})
                result=await session.call_tool('query',{'sql':'DELETE FROM sources'})
                assert result.isError
                result=await session.call_tool('sources',{'limit':-1})
                assert result.isError
    asyncio.run(run())
