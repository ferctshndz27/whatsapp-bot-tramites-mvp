# Correo (Poste.io) en el mismo VPS del bot — sin costo adicional

Opción elegida: reutilizar `srv1740939.hstgr.cloud` (el VPS que ya corre el bot de
trámites) en vez de crear un VPS nuevo. Cero cargo extra de Hostinger; a cambio,
correo y bot comparten IP y recursos.

## Por qué no usa `network_mode: host` (a diferencia de la referencia)

La [referencia](https://github.com/pablokbs/peladonerd/tree/master/terraform/3) da todo
el VPS a Poste.io. Aquí no se puede: **nginx ya es dueño de los puertos 80/443** sirviendo
`bot.esaumillan.cloud`. Este `docker-compose.yml` solo publica los puertos de protocolo de
correo (25/110/143/465/587/993/995 — nginx no los usa, sin conflicto) y deja el panel
web/webmail de Poste en `127.0.0.1:8081`, para que nginx lo exponga en
`mail.esaumillan.cloud` con su propio certificado, igual que ya hace con el bot.

## Cómo se despliega

Este `docker-compose.yml` viaja con el resto del repo por el mismo pipeline pull-based ya
configurado (`deploy/pull-deploy.sh`, cron cada 2 min): al llegar a `production`, el VPS
hace `docker compose up -d` aquí igual que en la raíz del repo, como un stack aparte
(nombre de proyecto `mailserver`, sin chocar con el del bot).

## Orden de pasos para activarlo

1. **DNS primero** — aplica `terraform/mailserver/` (agrega el registro A de
   `mail.esaumillan.cloud` apuntando a este mismo VPS, más MX/SPF/DMARC/CNAME). Espera a
   que propague antes de pedir el certificado (Let's Encrypt valida por HTTP).
2. **Deploy del contenedor** — ya ocurre solo en el siguiente `git push` a `main` (el pipeline
   existente lo levanta). Verifica con `docker ps --filter name=mail-poste`.
3. **Vhost de nginx** — sigue `nginx-mail.esaumillan.cloud.conf` de esta carpeta:
   ```bash
   ssh root@2.25.185.41
   cp mailserver/nginx-mail.esaumillan.cloud.conf /etc/nginx/sites-available/mail.esaumillan.cloud
   ln -s /etc/nginx/sites-available/mail.esaumillan.cloud /etc/nginx/sites-enabled/
   nginx -t && systemctl reload nginx
   certbot --nginx -d mail.esaumillan.cloud
   ```
4. **Configura Poste.io** — entra a `https://mail.esaumillan.cloud`, asistente inicial,
   crea el dominio virtual `esaumillan.cloud` y el primer buzón.
5. **DKIM** — *Virtual Domains → esaumillan.cloud → DKIM key*, copia el valor y agrégalo
   como registro TXT (plantilla comentada al final de `terraform/mailserver/dns.tf`).
6. **SMTP relay recomendado** — un IP compartido con un bot de WhatsApp de bajo volumen
   normalmente no tiene mala reputación, pero igual conviene configurar un relay
   (SendGrid/Mailgun) en *System Settings → Email Processing → Default SMTP Route* y
   validar el dominio remitente ahí, para evitar terminar en spam.

## Recursos

VPS actual: 2 vCPU, ~6 GB libres — de sobra para Poste.io sin ClamAV (por eso
`DISABLE_CLAMAV=TRUE`, ya que ClamAV es pesado y compite con el bot + MSSQL + n8n que ya
corren ahí). Si más adelante sobra CPU y quieres antivirus, quita esa línea del
`docker-compose.yml` de esta carpeta.
