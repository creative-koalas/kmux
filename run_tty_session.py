import asyncio
from src.kmux.terminal.tty_session import BlockPtySession

async def run_server(host="127.0.0.1", port=5555):
    server = await asyncio.start_server(handle_client, host, port)
    async with server:
        await server.serve_forever()

async def handle_client(reader, writer):
    tty_session = BlockPtySession()
    await tty_session.start()

    async def pump_output():
        while True:
            data = await tty_session._rx_q.get()
            print(data.decode(), end='', flush=True)
            if not data:
                break
            writer.write(data)
            await writer.drain()

    asyncio.create_task(pump_output())

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
