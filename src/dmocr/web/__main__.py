"""python -m dmocr.web

Localhost by default:

    python -m dmocr.web

Shareable through a tunnel (generates an access token and prints the URL to send):

    python -m dmocr.web --public
    cloudflared tunnel --url http://127.0.0.1:8000

Public mode still binds to 127.0.0.1 - the tunnel is what makes it reachable, and the
token is what makes that acceptable. `--host` is only for binding to a LAN address
directly, which also requires a token.
"""

from __future__ import annotations

import argparse
import os

from .auth import TOKEN_PARAM, generate_token


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the collateral review UI.")
    ap.add_argument("--host", default="127.0.0.1",
                    help="Bind address. Anything but loopback requires a token.")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--public", action="store_true",
                    help="Enable token access so the UI can be shared through a tunnel. "
                         "Shows a demo banner: synthetic documents only.")
    ap.add_argument("--token", default=os.environ.get("DMOCR_ACCESS_TOKEN"),
                    help="Shared access token. Generated if --public is given without one.")
    args = ap.parse_args()

    token = args.token
    if args.public and not token:
        token = generate_token()

    from .app import serve

    base = f"http://{args.host}:{args.port}"
    if token:
        print("=" * 72)
        print("PUBLIC MODE - shared token access")
        print("=" * 72)
        print(f"  Local URL : {base}/?{TOKEN_PARAM}={token}")
        print(f"  Token     : {token}")
        print()
        print("  Start a tunnel in another terminal, then share ITS url with the token:")
        print(f"    cloudflared tunnel --url {base}")
        print(f"    -> https://<name>.trycloudflare.com/?{TOKEN_PARAM}={token}")
        print()
        print("  DEMO USE ONLY. One shared token means no per-user identity and no")
        print("  revocation, and a token in a URL leaks through browser history.")
        print("  Do not upload real customer documents through a shared link -")
        print("  see docs/privacy/data-handling-policy.md.")
        print("=" * 72)
    else:
        print(f"Review UI on {base}  (localhost only)")

    serve(args.host, args.port, token=token)


if __name__ == "__main__":
    main()
