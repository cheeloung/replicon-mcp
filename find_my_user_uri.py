"""
find_my_user_uri.py — One-off setup helper: find your Replicon user URI.

server.py requires REPLICON_USER_URI to already be set in .env before it
will start, but the only way to find that value is to search Replicon for
yourself — so this script exists as a small, standalone step that only
needs REPLICON_BASE_URL and your auth (bearer token or company/username/
password) in .env, not the user URI itself.

Usage:
    python find_my_user_uri.py "Your Name"

Prints matching users' names and URIs. Copy the URI for yourself into
REPLICON_USER_URI in your .env file.
"""

import sys

from replicon_client import RepliconClient
import response_shapes


def main() -> None:
    if len(sys.argv) < 2:
        print('Usage: python find_my_user_uri.py "Your Name"')
        sys.exit(1)

    name_search = sys.argv[1]
    client = RepliconClient()
    raw = client.find_users(name_search=name_search)
    users = response_shapes.shape_user_list(raw)

    if not users:
        print(f"No users found matching '{name_search}'. Try a shorter or different search.")
        return

    print(f"Found {len(users)} user(s):\n")
    for user in users:
        print(f"  {user['name']}")
        print(f"    {user['uri']}\n")
    print("Copy your own URI above into REPLICON_USER_URI in your .env file.")


if __name__ == "__main__":
    main()
