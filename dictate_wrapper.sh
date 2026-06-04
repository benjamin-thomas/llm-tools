#!/bin/bash

PROVIDER="${1:?Usage: $0 <groq|openai|mistral>}"
SCRIPT_DIR="$(dirname "$0")"

case "$PROVIDER" in
    groq)    export GROQ_API_KEY="$(pass show GROQ_API_KEY)" ;;
    openai)  export OPENAI_API_KEY="$(pass show OPENAI_API_KEY)" ;;
    mistral) export MISTRAL_API_KEY="$(pass show MISTRAL_API_KEY)" ;;
    *)       echo "Unknown provider: $PROVIDER" >&2; exit 1 ;;
esac

exec /usr/bin/python3 "$SCRIPT_DIR/dictate.py" "$PROVIDER"
