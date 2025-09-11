from mcp.server.fastmcp import FastMCP

mcp = FastMCP()

@mcp.tool()
async def test():
    return "Hello Worldddd!"
