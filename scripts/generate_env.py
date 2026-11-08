"""Create .env with fresh random secrets. Standard library only; refuses to overwrite an existing file.

    python scripts/generate_env.py [--reviewer alice] [--reviewer bob]

Provider keys (ANTHROPIC_API_KEY, TAVILY_API_KEY) are left blank: paste them in yourself.
"""
import argparse
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reviewer", action="append", default=[], help="reviewer name (repeatable); default: owner")
    parser.add_argument("--force", action="store_true", help="overwrite an existing .env (destroys current secrets!)")
    args = parser.parse_args()

    target = ROOT / ".env"
    if target.exists() and not args.force:
        print(f"{target} already exists; not overwriting (use --force to replace it).", file=sys.stderr)
        return 1

    reviewers = args.reviewer or ["owner"]
    api_keys = ",".join(f"{name}:{secrets.token_urlsafe(32)}" for name in reviewers)

    template = (ROOT / ".env.example").read_text(encoding="utf-8")
    replacements = {
        "API_KEYS=generate-me": f"API_KEYS={api_keys}",
        "POSTGRES_PASSWORD=generate-me": f"POSTGRES_PASSWORD={secrets.token_hex(24)}",
        "REDIS_PASSWORD=generate-me": f"REDIS_PASSWORD={secrets.token_hex(24)}",
    }
    for old, new in replacements.items():
        if old not in template:
            print(f"template is missing '{old}'", file=sys.stderr)
            return 2
        template = template.replace(old, new)

    target.write_text(template, encoding="utf-8")
    try:
        target.chmod(0o600)
    except OSError:
        pass  # best effort (no-op semantics on Windows)
    print(f"Wrote {target}")
    print("Next: set ANTHROPIC_API_KEY and TAVILY_API_KEY in it, then `docker compose up --build -d`.")
    print("Your API keys (header X-API-Key) are in the API_KEYS line of .env.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
