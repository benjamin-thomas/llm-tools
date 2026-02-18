#!/bin/bash
export GROQ_API_KEY="$(pass show GROQ_API_KEY)"
exec python3 "$(dirname "$0")/dictate.py" "$@"
