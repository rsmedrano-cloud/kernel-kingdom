# Kernel Kingdom — reporte de máquinas remotas (protocolo v1)

Cada máquina del lab aparece como una isla del archipiélago. La PC que dibuja
el mundo **no manda código** a los servidores: se conecta por SSH con una llave
propia y restringida, y en el servidor esa llave ejecuta **un único script fijo**
que imprime un JSON. Este documento define ese JSON.

## Acceso

- Llave de la PC: `~/.ssh/kernel-kingdom` (ed25519), con el usuario que tengas en
  cada servidor.
- El script vive en el servidor, por ejemplo en `~/.local/bin/kernel-kingdom-report`
  (no hace falta sudo).
- En el servidor, una línea en `~/.ssh/authorized_keys` de ese usuario como esta:

  ```
  restrict,from="<IP de la PC>,<red de la casa>",command="/home/<usuario>/.local/bin/kernel-kingdom-report" ssh-ed25519 AAAA... kernel-kingdom
  ```

  `restrict` apaga reenvío de puertos, agente, X11 y PTY; `from=` limita desde
  dónde sirve la llave; con `command=` el comando que pida el cliente se ignora:
  siempre corre el script. Para quitar el acceso, se borra esa línea.
- El cliente usa el `ssh` del sistema, sin TTY, con la conexión reutilizada
  (`ControlMaster=auto`, `ControlPersist=10m`) y `BatchMode=yes`: si la llave no
  está autorizada, falla en lugar de preguntar.
- Frecuencia: una consulta cada 20 s por máquina (configurable). El cliente corta
  a los 10 s.

## Reglas para el script

1. Imprime **un solo objeto JSON** (UTF-8) por stdout y termina con código 0.
2. Tarda **menos de 1 s** y pesa **menos de 32 KB**.
3. **No incluye** líneas de comando, variables de entorno, usuarios, IPs, rutas
   de archivos ni nada que pueda tener secretos. Solo nombres de proceso (`comm`),
   nombres de contenedor y de servicio, y números.
4. Los contadores son **acumulados** (desde el arranque o desde que existe el
   proceso o el contenedor): el cliente calcula las tasas con la diferencia entre
   dos consultas. Así el script no tiene que dormir ni muestrear dos veces.
5. Si algo no se puede leer, el campo va en `null` (o la lista vacía): no hay que
   fallar por eso.
6. Si el script falla del todo: `{"v": 1, "error": "texto corto"}` y código 0.

## Campos

| Campo | Tipo | Qué es | De dónde sale (Linux) |
|---|---|---|---|
| `v` | int | Versión del protocolo: `1` | — |
| `host` | str | Nombre corto de la máquina | `hostname -s` |
| `t` | float | Hora del servidor, epoch en segundos | `time.time()` |
| `boot` | int | Hora de arranque, epoch | `/proc/stat` línea `btime` |
| `kernel` | str | Versión del kernel | `uname -r` |
| `arch` | str | Arquitectura | `uname -m` |
| `cpu.ticks_total` | int | Suma de todos los campos de la línea `cpu` | `/proc/stat` |
| `cpu.ticks_idle` | int | `idle + iowait` de la línea `cpu` | `/proc/stat` |
| `cpu.cores` | int | Núcleos lógicos | `nproc` |
| `cpu.load1` | float | Carga del último minuto | `/proc/loadavg` |
| `cpu.temp_c` | float/null | Temperatura de la CPU en °C | `/sys/class/thermal/thermal_zone*/temp` o hwmon (`k10temp`, `coretemp`, `cpu_thermal`) |
| `mem.total_mb` | int | Memoria total | `/proc/meminfo` `MemTotal` |
| `mem.available_mb` | int | Memoria disponible | `/proc/meminfo` `MemAvailable` |
| `mem.swap_total_mb` | int | Swap total | `/proc/meminfo` |
| `mem.swap_free_mb` | int | Swap libre | `/proc/meminfo` |
| `net.rx_bytes` | int | Bytes recibidos, todas las interfaces menos `lo` | `/proc/net/dev` |
| `net.tx_bytes` | int | Bytes enviados, ídem | `/proc/net/dev` |
| `disk.read_bytes` | int | Bytes leídos de discos físicos | `/proc/diskstats` (sectores × 512; solo discos enteros, no particiones ni `loop`/`ram`/`zram`) |
| `disk.write_bytes` | int | Bytes escritos, ídem | `/proc/diskstats` |
| `procs.count` | int | Procesos vivos (sin hilos del kernel) | `/proc/[0-9]*` |
| `procs.forks` | int | Procesos creados desde el arranque | `/proc/stat` línea `processes` |
| `groups` | list | Las aplicaciones pesadas (ver abajo), hasta 25 | `/proc/<pid>/stat` |
| `containers` | list | Contenedores (ver abajo) | `docker`/`podman` o cgroups |
| `services` | list | Servicios de interés (ver abajo) | `systemctl` |
| `sessions` | int | Sesiones de login abiertas ahora (SSH y locales), solo la cantidad | `loginctl list-sessions` (`who` puede dar 0 en algunos sistemas) |

### `groups`: aplicaciones

Un grupo es un proceso que cuelga directo de PID 1 (o del `systemd --user` de una
sesión) **más todos sus descendientes**. Así un Postgres con sus 20 workers o un
Jellyfin con sus transcodificadores cuentan como una sola cosa. Los procesos que
pertenecen a un contenedor van al contenedor, no a un grupo. Se informan los 25
grupos con más memoria, de mayor a menor.

| Campo | Tipo | Qué es |
|---|---|---|
| `name` | str | `comm` del proceso raíz del grupo (ej. `postgres`, `jellyfin`, `smbd`) |
| `procs` | int | Procesos del grupo |
| `rss_mb` | float | Memoria residente sumada |
| `cpu_s` | float | Segundos de CPU (user + system) acumulados por los procesos vivos del grupo |
| `started` | int | Epoch del proceso más viejo del grupo |

### `containers`

| Campo | Tipo | Qué es |
|---|---|---|
| `name` | str | Nombre del contenedor |
| `runtime` | str | `docker`, `podman`, `lxc`… |
| `state` | str | `running`, `paused`, `exited`… (informar también los detenidos de las últimas 24 h) |
| `started` | int/null | Epoch en que arrancó |
| `rss_mb` | float/null | Memoria (del cgroup) |
| `cpu_s` | float/null | Segundos de CPU acumulados (del cgroup) |
| `image` | str/null | Nombre corto de la imagen, sin registry ni tag (ej. `jellyfin`), opcional |

### `services`

Los servicios que valga la pena ver, de una lista fija que el script revisa en
cada máquina (`ssh`, `tailscaled`, `docker`, `smbd`, `ollama`…; solo los que existan).
Máximo 20.

| Campo | Tipo | Qué es |
|---|---|---|
| `name` | str | Nombre de la unidad sin `.service` |
| `active` | bool | Si está activa |
| `since` | int/null | Epoch del último cambio de estado |

## Ejemplo

```json
{
  "v": 1,
  "host": "servidor1",
  "t": 1793641200.4,
  "boot": 1793300000,
  "kernel": "6.12.48-1-lts",
  "arch": "x86_64",
  "cpu": {"ticks_total": 98213344, "ticks_idle": 91200110, "cores": 8, "load1": 0.42, "temp_c": 48.0},
  "mem": {"total_mb": 31890, "available_mb": 20114, "swap_total_mb": 4095, "swap_free_mb": 4095},
  "net": {"rx_bytes": 9123456789, "tx_bytes": 512345678},
  "disk": {"read_bytes": 912345678, "write_bytes": 4567890123},
  "procs": {"count": 214, "forks": 1880211},
  "groups": [
    {"name": "postgres", "procs": 12, "rss_mb": 812.5, "cpu_s": 3120.4, "started": 1793300042},
    {"name": "smbd", "procs": 3, "rss_mb": 64.0, "cpu_s": 88.1, "started": 1793300030}
  ],
  "containers": [
    {"name": "jellyfin", "runtime": "docker", "state": "running", "started": 1793350000,
     "rss_mb": 940.0, "cpu_s": 5120.7, "image": "jellyfin"},
    {"name": "immich-ml", "runtime": "docker", "state": "exited", "started": null,
     "rss_mb": null, "cpu_s": null, "image": "immich-machine-learning"}
  ],
  "services": [
    {"name": "sshd", "active": true, "since": 1793300020},
    {"name": "tailscaled", "active": true, "since": 1793300021}
  ],
  "sessions": 1
}
```

## Privacidad en la PC

- El nombre de la máquina, el usuario y la IP **no se muestran** en el mapa, la
  crónica, las capturas ni lo que se comparte: cada isla recibe un nombre inventado
  (estable, derivado de un hash). La dirección `usuario@máquina` solo vive en la
  config local y en la ventana de "Máquinas".
- Los errores de conexión se muestran como un motivo corto ("sin acceso", "no
  responde"…), nunca con el texto crudo de ssh (que puede traer IPs).
- Todo lo que no está en este contrato se descarta al recibirlo.

## Qué hace el juego con cada cosa

| Del reporte | En la isla |
|---|---|
| `groups` | Reinos: los más pesados son ciudades, los chicos aldeas. `procs` hace la ciudad más densa. |
| `containers` | Ciudadelas con murallas que crecen con el tiempo encendidas; detenido = ruinas. |
| `services` | Bastiones; inactivo = fuerte en silencio. |
| `cpu` (por diferencia) | Clima de la isla: tormenta con CPU alta; erupción si se sostiene. |
| `mem` | Niebla con la RAM llena; peste si se usa mucho swap. |
| `net` y `disk` (por diferencia) | Barcos y caravanas; la mina trabaja con el disco. |
| `procs.forks` (por diferencia) | Nacimientos de almas. |
| `boot` y `kernel` | Eras: reinicio = nueva era, kernel nuevo = nuevos dioses. |
| `sessions` | Emisarios en la costa. |
| Sin respuesta 3 veces seguidas | La isla queda a oscuras y en ruinas hasta que vuelva. |
