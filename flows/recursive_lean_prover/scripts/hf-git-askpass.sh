#!/usr/bin/env bash
set -eu

case "${1:-}" in
  *sername*) printf '%s\n' 'hf_user' ;;
  *) printf '%s\n' "${HUMANIZE_HF_TOKEN:?missing scoped Hugging Face token}" ;;
esac
