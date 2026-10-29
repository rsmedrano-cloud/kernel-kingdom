#!/usr/bin/env bash
# Sin instalar: ./run.sh            -> fondo de pantalla en segundo plano
#               ./run.sh window     -> la ventana (devuelve la terminal)
#               ./run.sh stop       -> detiene todo
#               ./run.sh run -f     -> todo en esta terminal, con logs (desarrollo)
cd "$(dirname "$(readlink -f "$0")")"
exec python3 -m reino "$@"
