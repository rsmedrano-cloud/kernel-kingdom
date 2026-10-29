#!/usr/bin/env bash
# Para desarrollar sin instalar: ./run.sh [run|window|doctor|chronicle|...]
cd "$(dirname "$(readlink -f "$0")")"
exec python3 -m reino "$@"
