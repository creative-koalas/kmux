import argparse

from .app import mcp, set_root_password


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("--root-password", type=str, default=None, required=False)
    args = parser.parse_args()
    set_root_password(password=args.root_password)

    mcp.run(transport="stdio")
