from mcp.server.fastmcp import FastMCP

fastmcp = FastMCP("addition")

@fastmcp.tool()
def add(a,b):
    return a + b

if "__name__"  ="__main__":
    fastmcp.run(transport="stdio")