import sys
import socket
import termios
import tty
import os
import select

def forward_input(host="127.0.0.1", port=5555):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((host, port))

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)  # raw input: no line buffering, no echo
        while True:
            r, _, _ = select.select([fd], [], [])
            if fd in r:
                data = os.read(fd, 1024)  # raw bytes from terminal
                if not data:
                    break
                sock.sendall(data)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sock.close()

if __name__ == "__main__":
    forward_input()
