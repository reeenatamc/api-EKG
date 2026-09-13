# Despliegue en Google Cloud para el piloto

Esta guía es para alguien que nunca ha usado Google Cloud. Cubre desde crear la cuenta hasta
apagar todo cuando el piloto termine. No sustituye al README ("Despliegue"), que describe el
Dockerfile, el docker-compose.yml, el Caddyfile y el .env.example de este repositorio: esta
guía es específicamente la parte de Google Cloud alrededor de eso.

Antes de seguirla, conviene leer completa la sección "Datos de salud" al final: hay dos pasos
que no son técnicos (consentimiento y comité de ética) y que deben resolverse antes de subir
la primera foto de un estudiante real.

## Lo que se verificó y cuándo

Los precios y condiciones de Google Cloud cambian. Todo lo que sigue se comprobó el
12 de septiembre de 2026 contra las páginas oficiales de Google Cloud (y, para Caddy y
Let's Encrypt, contra su documentación oficial y su repositorio público). Antes de dar por
buena una cifra para decidir un gasto real, vuelve a mirar la fuente citada: puede haber
cambiado.

- Condiciones de la prueba sin coste económico: 300 dólares de crédito, válidos 90 días.
  Pide una tarjeta al registrarse (o un método de pago equivalente), pero solo hace una
  retención temporal, no un cobro, y no hay cobro automático al terminar la prueba: esta
  acaba cuando se agota el crédito, pasan los 90 días o decides pasarte tú misma a una
  cuenta de pago, lo que ocurra primero.
  Fuente: [Free Trial FAQs, Google Cloud](https://cloud.google.com/signup-faqs) y
  [Programa gratuito de Google Cloud](https://docs.cloud.google.com/free/docs/free-cloud-features),
  consultadas el 12 de septiembre de 2026.
- southamerica-east1 (São Paulo) sí ofrece e2-standard-4 (4 vCPU, 16 GB). Precio bajo
  demanda: 0,2127408 USD por hora, unos 155,30 USD al mes si estuviera prendida las 730
  horas del mes (en Iowa, us-central1, el mismo tipo de máquina cuesta 0,13402284 USD por
  hora: São Paulo sale alrededor de 1,6 veces más caro, que es la prima habitual de esa
  región).
  Fuente: [Precios de máquinas virtuales de uso general, Google Cloud](https://cloud.google.com/products/compute/pricing/general-purpose),
  tabla de precios de la familia E2 con la región cambiada a Sao Paulo
  (southamerica-east1), consultada el 12 de septiembre de 2026.
- Disco persistente estándar en São Paulo: 0,06 USD por GiB al mes, así que 100 GB salen en
  unos 6 USD al mes. El balanceado (pd-balanced, el que gcloud usa por defecto si no se
  indica otro) sale a 0,15 USD por GiB al mes, unos 15 USD al mes para 100 GB. Para este
  piloto (una base de datos pequeña, sin mucha escritura simultánea) el estándar alcanza de
  sobra.
  Fuente: [Precios de Persistent Disk e Hyperdisk, Google Cloud](https://cloud.google.com/compute/disks-image-pricing),
  región Sao Paulo, consultada el 12 de septiembre de 2026.
- IP estática: si está asignada a una instancia (encendida o apagada), cuesta 0,005 USD por
  hora, unos 3,65 USD al mes. Si se reserva pero no está asignada a nada, cuesta el doble,
  0,01 USD por hora (7,30 USD al mes). Hay una hora al mes sin costo por cuenta.
  Fuente: [Precios de red, Google Cloud](https://cloud.google.com/vpc/network-pricing),
  tabla de direcciones IP externas, consultada el 12 de septiembre de 2026.
- Con la máquina prendida el mes entero: 155,30 (cómputo) + 6 (disco estándar de 100 GB) +
  3,65 (IP estática) es alrededor de 165 USD al mes. El crédito de 300 USD cubre menos de
  dos meses de máquina encendida sin parar. Si la VM se apaga fuera de las horas de clase
  (ver la sección "Apagar todo"), el gasto real de un piloto de unas semanas es mucho menor:
  solo el cómputo se detiene al apagar la instancia; el disco y la IP siguen cobrándose
  mientras existan.

## a) En la consola, lo que hace Renata en persona

Esto no se puede automatizar por buenas razones (identidad, tarjeta, aceptación de
condiciones), así que son pasos manuales:

1. Entra a [cloud.google.com](https://cloud.google.com) con la cuenta de Google que quieras
   usar para el piloto y activa la prueba sin coste económico. Pide una tarjeta, pero como
   se explicó arriba no cobra nada mientras estés dentro de los 300 USD y los 90 días.
2. Crea un proyecto nuevo solo para este piloto (por ejemplo `ekg-piloto`), en vez de usar
   el proyecto de pruebas que la consola crea por defecto. Así, cuando termine el piloto,
   borrar el proyecto borra todo lo que contiene sin arriesgar nada de otro trabajo.
3. Activa una alerta de presupuesto: en la consola, "Facturación" → "Presupuestos y
   alertas" → "Crear presupuesto". Ponle un monto bajo, por ejemplo 20 o 30 USD, y dos o
   tres umbrales (50 %, 90 %, 100 %) para que te avise por correo si el gasto se acelera
   por algo que no esperabas (una VM que quedó prendida, por ejemplo).
4. Activa la API de Compute Engine: en la consola, "APIs y servicios" → "Habilitar APIs y
   servicios" → busca "Compute Engine API" → "Habilitar". (El comando equivalente de
   `gcloud` está en la sección c, por si prefieres hacerlo desde la terminal una vez tengas
   el SDK instalado.)

## b) Instalar gcloud en macOS y autenticarse

En tu MacBook, no en la VM:

```bash
brew install --cask google-cloud-sdk
gcloud init
```

`gcloud init` abre el navegador para iniciar sesión con la misma cuenta de Google del paso
anterior, y te deja elegir el proyecto (`ekg-piloto`) y una región/zona por defecto. Cuando
pregunte por la región, usa `southamerica-east1` y una zona como `southamerica-east1-a`.

Si no lo hizo `gcloud init`, activa la API de Compute Engine desde la terminal:

```bash
gcloud services enable compute.googleapis.com
```

## c) Crear la VM

Esta sección crea una red propia para el piloto en vez de usar la red "default" de Google
Cloud, porque la red default trae de fábrica una regla de firewall que abre el puerto 22
(SSH) a cualquier IP de Internet, y el pedido es que solo 80 y 443 queden abiertos al
mundo. Con una red propia, no hay ninguna regla implícita: se abre exactamente lo que se
indique.

```bash
# Variables para no repetirlas en cada comando
export PROJECT_ID=ekg-piloto
export REGION=southamerica-east1
export ZONE=southamerica-east1-a
export VM_NAME=ekg-piloto-vm

gcloud config set project $PROJECT_ID

# Red propia, sin reglas de firewall implícitas
gcloud compute networks create ekg-piloto-vpc --subnet-mode=custom

gcloud compute networks subnets create ekg-piloto-subnet \
  --network=ekg-piloto-vpc \
  --region=$REGION \
  --range=10.10.0.0/24

# Solo 80 y 443, abiertos a cualquiera (ahí escucha Caddy)
gcloud compute firewall-rules create ekg-piloto-allow-web \
  --network=ekg-piloto-vpc \
  --direction=INGRESS \
  --action=ALLOW \
  --rules=tcp:80,tcp:443 \
  --source-ranges=0.0.0.0/0

# SSH solo desde el rango de Identity-Aware Proxy de Google, no desde cualquier IP.
# 35.235.240.0/20 es el rango fijo que Google documenta para las conexiones de IAP.
gcloud compute firewall-rules create ekg-piloto-allow-iap-ssh \
  --network=ekg-piloto-vpc \
  --direction=INGRESS \
  --action=ALLOW \
  --rules=tcp:22 \
  --source-ranges=35.235.240.0/20

# IP estática, para no perderla si la VM se reinicia o se detiene
gcloud compute addresses create ekg-piloto-ip --region=$REGION
gcloud compute addresses describe ekg-piloto-ip --region=$REGION --format='get(address)'
# Anota la IP que imprime este último comando: la necesitas para el DNS o para sslip.io/nip.io.

# La VM: Ubuntu 24.04 LTS, 4 vCPU / 16 GB, disco de 100 GB
gcloud compute instances create $VM_NAME \
  --zone=$ZONE \
  --machine-type=e2-standard-4 \
  --image-family=ubuntu-2404-lts-amd64 \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size=100GB \
  --boot-disk-type=pd-standard \
  --network=ekg-piloto-vpc \
  --subnet=ekg-piloto-subnet \
  --address=ekg-piloto-ip \
  --tags=ekg-piloto
```

Para entrar por SSH, `gcloud` genera y usa una llave propia (nunca contraseña) y, con
`--tunnel-through-iap`, pasa por el firewall de IAP en vez de necesitar el puerto 22
abierto al mundo:

```bash
gcloud compute ssh $VM_NAME --zone=$ZONE --tunnel-through-iap
```

La primera vez puede pedir permiso para generar un par de llaves SSH; acepta. Si la consola
avisa que falta el rol de IAP (`roles/iap.tunnelResourceAccessor`) para tu propia cuenta,
otórgalo desde IAM y vuelve a intentar.

`--boot-disk-type=pd-standard` es el más barato (ver la tabla de precios arriba); si
prefieres el `pd-balanced` que gcloud usaría por defecto, quita esa línea o cámbiala, a
costa de unos 9 USD más al mes para 100 GB.

## d) Instalar Docker, clonar el repo, preparar el .env y bajar los pesos

Ya dentro de la VM (después del `gcloud compute ssh` de arriba):

```bash
# Docker Engine + el plugin de Compose, con el script oficial de Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo usermod -aG docker $USER
# Cierra la sesión SSH y vuelve a entrar para que el grupo "docker" tome efecto.

git clone <url-del-repo-api-EKG> api-EKG
cd api-EKG

cp .env.example .env
nano .env   # o el editor que prefieras
```

En el `.env`, como mínimo (los comentarios del propio archivo explican cada variable):

- `SECRET_KEY`: generarla con `python3 -c "import secrets; print(secrets.token_urlsafe(50))"`.
- `DEBUG=0` (o quitar la línea, que es lo mismo).
- `ALLOWED_HOSTS` y `CSRF_TRUSTED_ORIGINS` con el dominio que se vaya a usar (ver la
  sección siguiente si todavía no lo tienes).
- `DATABASE_ENGINE=postgresql` y las `POSTGRES_*` (usuario, contraseña, nombre de la base).
- `DOMAIN` con el mismo dominio.
- `ECGFOUNDER_WEIGHTS_DIR=/data/weights`.
- Las `EMAIL_*` del proveedor SMTP que uses (Resend, según el README).
- `WORKER_REPLICAS=2` y `OMP_NUM_THREADS=2` (4 vCPU repartidos entre dos réplicas del
  worker, siguiendo la misma cuenta que ya explica `.env.example`).
- Las variables nuevas de este piloto que agrega la Parte 1 de este cambio:
  `AUTH_IP_THROTTLE_RATE`, `GUNICORN_WORKER_CLASS` y `GUNICORN_THREADS` (los valores por
  defecto ya sirven; solo hace falta tocarlos si el aula resulta más grande de lo previsto).

Los pesos de ECGFounder (unos 700 MB) no se bajan a mano: el contenedor `worker` los baja
solo la primera vez que arranca (`docker/entrypoint-worker.sh`). Eso pasa en el siguiente
paso, al levantar los contenedores.

## e) Dominio para HTTPS

Caddy pide el certificado solo con que `DOMAIN` en el `.env` sea un nombre que de verdad
resuelva a la IP de la VM. Hay dos caminos:

### Opción con dominio propio (recomendada si el piloto puede durar más de unas semanas)

Comprar un dominio barato (un .com o similar sale por unos 10 a 15 USD al año en
Namecheap, Porkbun o Squarespace Domains, que es quien compró el antiguo Google Domains) y
crear un registro A apuntando a la IP estática del paso c. Es la opción sin ninguna
dependencia de terceros gratuitos, y el costo es insignificante frente al de la VM.

### Opción temporal y gratuita: sslip.io o nip.io

Estos servicios resuelven un nombre como `IP-con-guiones.sslip.io` directamente a esa IP,
sin que tengas que configurar nada de DNS. Si la IP estática fuera, por ejemplo,
`34.95.10.20`, el dominio sería `34-95-10-20.sslip.io`, y eso es lo que va en `DOMAIN`.

Con una salvedad importante, verificada hoy: Let's Encrypt sí emite certificados para
subdominios de sslip.io y nip.io en general, pero sslip.io agotó su cuota semanal de
certificados con Let's Encrypt en febrero de 2026 por el volumen de tráfico automatizado
que recibe ese dominio compartido por miles de usuarios, y quedó gente sin poder emitir
certificado nuevo durante ese episodio. La cuota se ha subido varias veces (de 50 a 250.000
certificados por semana), pero al ser un dominio compartido, el riesgo de toparse con el
límite en un mal momento (como el primer día de clase) no es cero.

Fuentes: [issue #108, repositorio de sslip.io en GitHub](https://github.com/cunnie/sslip.io/issues/108)
y [límites de tasa de Let's Encrypt](https://letsencrypt.org/docs/rate-limits/), consultadas
el 12 de septiembre de 2026.

Recomendación práctica: probar primero con sslip.io; si Caddy no logra emitir el
certificado (se ve en `docker compose logs caddy`), cambiar `DOMAIN` a la variante con
nip.io (mismo formato, `IP-con-guiones.nip.io`) y reiniciar Caddy; si ninguna de las dos
funciona ese día, es la señal de comprar el dominio propio de la sección anterior en vez de
seguir dependiendo de un recurso compartido y gratuito justo para el piloto real.

## f) Arrancar, comprobar, crear la superusuaria y programar la copia de seguridad

```bash
docker compose build
docker compose up -d
docker compose logs -f worker   # esperar a que termine de bajar los pesos de ECGFounder
```

Comprobar que responde:

```bash
curl -f https://TU-DOMINIO/healthz/
docker compose ps
```

Crear la cuenta de administración del panel:

```bash
docker compose exec api python manage.py createsuperuser
```

Copia de seguridad diaria a Cloud Storage. Primero, un bucket para guardarla (una sola vez):

```bash
gcloud storage buckets create gs://ekg-piloto-backups --location=$REGION
```

Luego, una entrada de `cron` en la VM (`crontab -e`) que corre el script que ya trae el
repositorio y sube el resultado:

```
0 3 * * * cd /home/tu-usuario/api-EKG && ./scripts/backup.sh /var/backups/ekg && gcloud storage cp /var/backups/ekg/*"$(date +\%Y\%m\%d)"* gs://ekg-piloto-backups/
```

`scripts/backup.sh` guarda un volcado de Postgres y un `tar` de `media/`, sin cifrar (ver
README, "Copias de seguridad"): considera cifrar antes de subir si las fotos van a
permanecer ahí más tiempo del que dure el piloto.

## g) Apuntar la app al dominio nuevo

La URL de la API queda compilada dentro del binario nativo de la app (así lo dice el propio
comentario de `DOMAIN` en el Dockerfile de este repositorio: "API_BASE_URL is compiled
in"). Esto quiere decir que cambiar de dominio, o pasar de una IP de prueba a un dominio
definitivo, no es un cambio que la app recoja sola: hay que actualizar esa URL en la
configuración del proyecto app-EKG y volver a compilar y distribuir la app (una nueva
build de Expo/EAS, o el APK/IPA correspondiente) antes de que los estudiantes puedan usarla
contra el servidor nuevo. Si el proyecto usa actualizaciones OTA de Expo y ese valor se lee
desde JavaScript en tiempo de ejecución en vez de estar fijo en la build nativa, una
actualización OTA podría bastar: hay que revisarlo en app-EKG, no se puede asumir aquí.

## h) Apagar todo al terminar el piloto

Para pausar entre una sesión de clase y la siguiente, sin perder nada:

```bash
docker compose down       # dentro de la VM, detiene los contenedores
gcloud compute instances stop $VM_NAME --zone=$ZONE   # deja de cobrarse el cómputo
```

Mientras la VM esté detenida, se sigue pagando el disco (unos 6 USD al mes con
`pd-standard` de 100 GB) y la IP estática asignada (unos 3,65 USD al mes), pero no el
cómputo, que es la parte cara.

Para terminar el piloto del todo y dejar de gastar crédito:

```bash
gcloud compute instances delete $VM_NAME --zone=$ZONE
gcloud compute addresses delete ekg-piloto-ip --region=$REGION
gcloud compute firewall-rules delete ekg-piloto-allow-web ekg-piloto-allow-iap-ssh
gcloud compute networks subnets delete ekg-piloto-subnet --region=$REGION
gcloud compute networks delete ekg-piloto-vpc
```

Antes del primer comando, asegúrate de que la última copia de seguridad ya está en
`gs://ekg-piloto-backups`, porque borrar la VM borra también su disco y todo lo que no se
haya sacado de ahí. El bucket de Cloud Storage se puede dejar (sigue cobrándose, pero muy
poco) o borrarlo también con `gcloud storage rm --recursive gs://ekg-piloto-backups` una
vez que las copias estén a salvo en otro lugar. Si además quieres asegurarte de que no
quede nada activo en el proyecto, la manera más simple es borrar el proyecto entero desde
la consola ("IAM y administración" → "Configuración" → "Inhabilitar proyecto"), que apaga
la facturación de todo lo que contenga.

## i) Si la espera en clase sigue siendo un problema: Cloud Run Jobs (sin implementar)

Esto no se implementa ahora. Se deja documentado para cuando (o si) la cola de análisis en
clase siga siendo un cuello de botella incluso con las mejoras de la Parte 1 de este
cambio.

La idea sería que, en vez de dos contenedores `worker` fijos corriendo todo el tiempo en la
misma VM, cada análisis dispare una ejecución de un Cloud Run Job: una tarea que arranca,
corre `run_worker --once` (o un comando equivalente que procese un solo estudio) y termina,
y por la que solo se paga mientras está corriendo.

Lo que cambiaría:

- El `worker` dejaría de ser un proceso de vida larga que hace polling de la base de
  datos; en su lugar, algo (la propia API, o una Cloud Function disparada por Pub/Sub)
  tendría que invocar la ejecución del Job cuando se encole un estudio nuevo (API
  `jobs.run` de Cloud Run).
- Los pesos de ECGFounder y el checkout de Open-ECG-Digitizer, que hoy viven en un volumen
  de Docker compartido entre réplicas, tendrían que empaquetarse en la imagen del Job o
  bajarse en cada ejecución (con el costo de tiempo que eso añade a cada arranque, el
  problema de "arranque en frío" que se explica abajo).
- Postgres tendría que quedar accesible desde Cloud Run, lo que en la práctica significa
  Cloud SQL en vez de un contenedor `db` en la misma VM (o un conector de VPC hacia la VM,
  más frágil).

Límites verificados hoy (12 de septiembre de 2026) contra la documentación oficial de
Cloud Run:

- CPU máxima por tarea: 8 vCPU. Memoria: para 4 vCPU, de 2 a 16 GiB; para 6 vCPU, de 4 a
  24 GiB. El uso medido de este servicio (hasta 6 GB) entra cómodo en cualquiera de las
  dos combinaciones.
  Fuente: [Configura los límites de CPU para los trabajos, Cloud Run](https://cloud.google.com/run/docs/configuring/jobs/cpu).
- Tiempo máximo por tarea: 10 minutos por defecto, configurable hasta 168 horas (7 días)
  para tareas sin GPU. Un análisis de 40 a 60 segundos deja margen de sobra.
  Fuente: [Cómo establecer el tiempo de espera de las tareas, Cloud Run](https://cloud.google.com/run/docs/configuring/task-timeout).
- Se cobra por incrementos de 100 milisegundos mientras la tarea corre, con un mínimo de
  1 minuto facturado por ejecución. En southamerica-east1 (que está en el nivel 2 de
  precios de Cloud Run, más caro que Iowa): 0,0000216 USD por vCPU-segundo y 0,0000024 USD
  por GiB-segundo.
  Fuente: [Precios de Cloud Run](https://cloud.google.com/run/pricing), tabla "Empleo"
  (Jobs) con la región cambiada a Sao Paulo.

Costo estimado por análisis, con 4 vCPU y 6 GiB durante 60 segundos en São Paulo:
CPU: 4 × 60 × 0,0000216 = 0,005184 USD.
RAM: 6 × 60 × 0,0000024 = 0,000864 USD.
Total: unos 0,006 USD por análisis (seis milésimas de dólar), sin contar el nivel sin
costo económico mensual que probablemente cubra la mayoría del uso de un piloto pequeño.

Esto es muchísimo más barato por análisis que una VM dedicada corriendo 24 horas, pero trae
un problema nuevo: el arranque en frío. Cada ejecución tendría que cargar PyTorch, el
modelo ECGFounder (unos 700 MB) y el checkout del digitalizador antes de poder procesar
nada, lo que en la práctica puede añadir varias decenas de segundos a cada estudio a menos
que se mantenga al menos una instancia mínima caliente (lo cual vuelve a cobrar de forma
continua y le quita buena parte de la ventaja de costo). Migrar a esto tiene sentido si el
problema real termina siendo de memoria o de cantidad de máquinas simultáneas, no si el
problema es la latencia del primer análisis del día.

Con GPU, el worker ya está preparado aunque la plataforma no está decidida: la imagen
`docker/Dockerfile.worker-gpu` y la variable `ECG_DEVICE=cuda` (ver README, "Worker con
GPU"). En Colab con una T4 un estudio tardó unos 17 s en caliente y 26 s en frío, frente a
95 s en CPU, con la misma calidad en 24 de 24 imágenes. Eso reduce mucho el arranque en frío
descrito arriba, pero los precios de GPU en Cloud Run o en una VM no están verificados en
esta guía.

## j) Datos de salud: región, transferencia internacional y comité de ética

Tres cosas que no son técnicas pero que condicionan si este despliegue se puede usar con
estudiantes de verdad:

- Las fotos de electrocardiogramas son datos de salud, una categoría sensible bajo
  cualquier ley de protección de datos razonable, incluida la ecuatoriana. southamerica-east1
  guarda los datos en Brasil, no en Ecuador: eso es una transferencia internacional de
  datos, y el formulario de consentimiento que firmen los estudiantes debe decirlo
  explícitamente (dónde se almacenan los datos, por qué motivo, quién es el proveedor y
  qué protecciones ofrece), no dar por sentado que "la nube" no tiene ubicación.
- El consentimiento informado debe cubrir, además de la ubicación: qué se recolecta (la
  imagen del electrocardiograma y los resultados del análisis), cuánto tiempo se conserva,
  quién puede verlo (quien investiga, no terceros), y que la cuenta y sus datos se pueden
  borrar a pedido (el endpoint `DELETE /auth/account/` de este mismo backend ya cumple esa
  parte: bórralo si el estudiante lo pide, y dilo en el consentimiento).
- Antes de recolectar el primer dato real, el piloto necesita la aprobación del comité de
  ética de la institución. Esta guía no sustituye ese trámite: los pasos técnicos de arriba
  pueden completarse en paralelo, pero no subas fotos reales de estudiantes hasta tener esa
  aprobación por escrito.
