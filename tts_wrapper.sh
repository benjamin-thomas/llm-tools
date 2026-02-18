#!/bin/bash
export OPENAI_API_KEY="$(pass show OPENAI_API_KEY)"
exec python3 "$(dirname "$0")/tts.py" "$@"
