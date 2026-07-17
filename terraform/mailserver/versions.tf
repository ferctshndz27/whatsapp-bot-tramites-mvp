terraform {
  required_version = ">= 1.5"
  required_providers {
    hostinger = {
      source  = "hostinger/hostinger"
      version = "~> 0.1"
    }
  }
}

provider "hostinger" {
  api_token = var.hostinger_api_token
}
