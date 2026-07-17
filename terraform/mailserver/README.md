# DNS para correo (Poste.io) — Terraform + Hostinger

Este módulo **ya no crea un VPS**. Se decidió reutilizar el VPS existente
(`srv1740939.hstgr.cloud`, el que corre el bot de trámites) en vez de pagar uno nuevo —
ver [`../mailserver/README.md`](../mailserver/README.md) para el resto de la instalación
(el contenedor de Poste.io y el vhost de nginx).

Terraform aquí solo administra los registros DNS, usando el
[provider oficial `hostinger/hostinger`](https://registry.terraform.io/providers/hostinger/hostinger/latest/docs)
— el mismo proveedor donde ya vive el dominio `esaumillan.cloud` (confirmé que sus
nameservers ya son de Hostinger, así que no hace falta ningún otro proveedor de DNS).

## Qué crea

| Registro | Para qué |
|---|---|
| `mail.esaumillan.cloud` A → IP del VPS existente | Resolver el subdominio de correo |
| `@` MX → `mail.esaumillan.cloud` (prioridad 10, embebida en el valor) | Que el dominio reciba correo |
| `@` TXT (SPF) | `v=spf1 mx ~all` |
| `_dmarc` TXT (DMARC) | Política de reportes |
| `smtp`/`pop`/`imap` CNAME → `mail.esaumillan.cloud` | Clientes que buscan esos nombres por defecto |

Ninguno de estos toca los registros A que ya existen en la raíz del dominio (Hostinger
trata cada combinación nombre+tipo como independiente).

## Antes de aplicar

1. Token de API: hPanel → cuenta → API → genera uno. Va en `terraform.tfvars` (gitignored)
   o `TF_VAR_hostinger_api_token` — nunca en el chat ni en el repo.
2. Copia `terraform.tfvars.example` a `terraform.tfvars`.
3. `terraform plan` y revisa el diff — esto modifica el DNS público de tu dominio.

```bash
cd terraform/mailserver
terraform init
terraform plan
terraform apply
```

## Después de aplicar

Sigue la secuencia completa en [`../mailserver/README.md`](../mailserver/README.md):
vhost de nginx, certbot, configuración inicial de Poste.io, DKIM y SMTP relay.
