#!/bin/bash
export OPENAI_API_KEY="$(pass show OPENAI_API_KEY)"
export MISTRAL_API_KEY="$(pass show MISTRAL_API_KEY)"
exec python3 "$(dirname "$0")/read_aloud_web.py" "$@"
