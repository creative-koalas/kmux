import asyncio
from src.kmux.terminal.pty_session import PtySession

async def run_server(host="127.0.0.1", port=5555):
    server = await asyncio.start_server(handle_client, host, port)
    async with server:
        await server.serve_forever()

async def handle_client(reader, writer):
    tty_session = PtySession(
        on_new_output_callback=lambda data: print(data.decode(), end='', flush=True),
        on_session_closed_callback=lambda: print("Session closed")
    )
    
    await tty_session.start()

    try:
        while True:
            data = await reader.read(1024)
            if not data:
                break
            await tty_session._write_bytes(data)
    finally:
        writer.close()
        await writer.wait_closed()
        tty_session.stop()

if __name__ == "__main__":
    asyncio.run(run_server())
