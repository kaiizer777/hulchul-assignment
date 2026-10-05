variable "database_url" {
  type      = string
  sensitive = true
}

variable "groq_api_key" {
  type      = string
  sensitive = true
}

variable "upstash_redis_rest_url" {
  type      = string
  sensitive = true
}

variable "upstash_redis_rest_token" {
  type      = string
  sensitive = true
}

variable "browser_ws_endpoint" {
  type      = string
  sensitive = true
}

variable "auth_password_hash" {
  type      = string
  sensitive = true
}

variable "auth_agent_password" {
  type      = string
  sensitive = true
  default   = ""
}

variable "cors_origins" {
  type    = string
  default = "http://localhost:3051,http://127.0.0.1:3051"
}

variable "frontend_url" {
  type    = string
  default = "http://localhost:3051"
}

variable "image_tag" {
  type    = string
  default = "latest"
}

variable "opencode_api_key" {
  type      = string
  sensitive = true
  default   = ""
}

variable "opencode_base_url" {
  type    = string
  default = "https://opencode.ai/zen/v1"
}

variable "opencode_model" {
  type    = string
  default = "space-bunny-free"
}

variable "erp_base_url" {
  type    = string
  default = "https://hulchul-frontend.sufiyanx.workers.dev"
}

