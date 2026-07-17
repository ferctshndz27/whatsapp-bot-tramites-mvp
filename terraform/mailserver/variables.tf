variable "hostinger_api_token" {
  description = "API token de Hostinger (hPanel > Cuenta > API). No lo pongas en texto plano: usa TF_VAR_hostinger_api_token o terraform.tfvars (gitignored)."
  type        = string
  sensitive   = true
}

variable "domain" {
  description = "Dominio raíz ya administrado en Hostinger (mismo que usa el bot de trámites)."
  type        = string
  default     = "esaumillan.cloud"
}

variable "mail_subdomain" {
  description = "Subdominio donde vivirá el correo (registro A/MX y hostname en nginx)."
  type        = string
  default     = "mail"
}

variable "existing_vps_ip" {
  description = "IP del VPS que YA existe y corre el bot — Poste.io se instala ahí mismo, sin crear un VPS nuevo."
  type        = string
  default     = "2.25.185.41"
}

variable "dmarc_report_email" {
  description = "Correo que recibirá los reportes agregados de DMARC."
  type        = string
  default     = "dmarc-reports@esaumillan.cloud"
}
