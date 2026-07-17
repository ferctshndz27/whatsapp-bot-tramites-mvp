# Todos estos registros conviven sin conflicto con los A records que ya
# tiene la raíz del dominio (misma zona, distinto name+type) — Hostinger
# gestiona name+type como la unidad atómica, no el registro individual.

resource "hostinger_dns_record" "mail_a" {
  zone  = var.domain
  name  = var.mail_subdomain
  type  = "A"
  value = var.existing_vps_ip
  ttl   = 14400
}

resource "hostinger_dns_record" "mx" {
  zone = var.domain
  name = "@"
  type = "MX"
  # La prioridad va embebida en el contenido (no hay campo aparte en la API
  # de Hostinger) — sigue la sintaxis clásica de zonefile "prioridad host".
  value = "10 ${var.mail_subdomain}.${var.domain}."
  ttl   = 14400
}

resource "hostinger_dns_record" "spf" {
  zone  = var.domain
  name  = "@"
  type  = "TXT"
  value = "v=spf1 mx ~all"
  ttl   = 14400
}

resource "hostinger_dns_record" "dmarc" {
  zone  = var.domain
  name  = "_dmarc"
  type  = "TXT"
  value = "v=DMARC1; p=none; rua=mailto:${var.dmarc_report_email}"
  ttl   = 14400
}

resource "hostinger_dns_record" "smtp_cname" {
  zone  = var.domain
  name  = "smtp"
  type  = "CNAME"
  value = "${var.mail_subdomain}.${var.domain}."
  ttl   = 14400
}

resource "hostinger_dns_record" "pop_cname" {
  zone  = var.domain
  name  = "pop"
  type  = "CNAME"
  value = "${var.mail_subdomain}.${var.domain}."
  ttl   = 14400
}

resource "hostinger_dns_record" "imap_cname" {
  zone  = var.domain
  name  = "imap"
  type  = "CNAME"
  value = "${var.mail_subdomain}.${var.domain}."
  ttl   = 14400
}

# DKIM: Poste.io genera su propia clave la primera vez que agregas el
# dominio en Virtual Domains. No existe antes de eso, así que no se puede
# provisionar por Terraform de entrada — se agrega a mano una vez generada:
#
# resource "hostinger_dns_record" "dkim" {
#   zone  = var.domain
#   name  = "poste-dkim._domainkey"   # el nombre exacto lo da Poste.io
#   type  = "TXT"
#   value = "v=DKIM1; k=rsa; p=..."   # pégalo desde Virtual Domains > tu dominio > DKIM key
#   ttl   = 14400
# }
