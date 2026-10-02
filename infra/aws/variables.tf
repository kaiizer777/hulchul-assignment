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

variable "cors_origins" {
  type    = string
  default = "http://localhost:3051,http://127.0.0.1:3051,https://*.pages.dev,*"
}

variable "frontend_url" {
  type    = string
  default = "http://localhost:3051"
}

variable "image_tag" {
  type    = string
  default = "latest"
}

